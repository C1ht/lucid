"""Live download progress: bytes written and speed are measured directly
(snapshot the watched directory, diff against a baseline taken before the
job started, so pre-existing files in a shared directory are never counted).
The completion percentage is only an estimate where we have duration +
quality data to estimate a target size from (currently: Qobuz) -- lossless
formats have no single "correct" compressed size without the real encoder
output, so treat the bar as approximate, not exact."""

import asyncio
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

_LOSSY_BITRATES = {0: 128_000, 1: 320_000}  # bits/sec, matches the quality table in Settings
POLL_INTERVAL_SECONDS = 1.0


# Per quality tier: (max bit depth, max sampling rate in Hz) Qobuz will deliver.
_LOSSLESS_CAPS = {2: (16, 44100), 3: (24, 96000), 4: (24, 192000)}


def estimate_track_bytes(
    duration_sec: Optional[float],
    quality: int,
    track_bit_depth: Optional[int] = None,
    track_sample_rate: Optional[float] = None,
) -> Optional[int]:
    if not duration_sec or duration_sec <= 0:
        return None
    if quality in _LOSSY_BITRATES:
        return int(duration_sec * _LOSSY_BITRATES[quality] / 8)
    # Lossless (FLAC): no exact answer without the real encoder output. Use the
    # track's own max bit depth / sample rate, capped by the quality tier
    # (a 24/192 master downloaded at tier 3 arrives as 24/96), then apply a
    # typical FLAC compression ratio (~60% of raw PCM). Approximate.
    cap_depth, cap_rate = _LOSSLESS_CAPS.get(quality, (24, 96000))
    bit_depth = min(track_bit_depth or cap_depth, cap_depth)
    rate = track_sample_rate or cap_rate
    # Qobuz reports rates in kHz (e.g. 96.0); normalise to Hz before capping.
    if rate < 1000:
        rate *= 1000
    sample_rate = min(rate, cap_rate)
    raw_bytes = duration_sec * sample_rate * bit_depth * 2 / 8
    return int(raw_bytes * 0.6)


def estimate_total_bytes(meta: dict, quality: int) -> Optional[int]:
    """meta: a raw Qobuz response for a track OR an album (has tracks.items)."""
    items = (meta.get("tracks") or {}).get("items")
    if items:
        total = 0
        found = False
        for item in items:
            est = estimate_track_bytes(
                item.get("duration"), quality,
                item.get("maximum_bit_depth"), item.get("maximum_sampling_rate"),
            )
            if est:
                total += est
                found = True
        return total if found else None
    return estimate_track_bytes(
        meta.get("duration"), quality,
        meta.get("maximum_bit_depth"), meta.get("maximum_sampling_rate"),
    )


def _snapshot(watch_dir: Path) -> dict[Path, int]:
    if not watch_dir.exists():
        return {}
    return {p: p.stat().st_size for p in watch_dir.rglob("*") if p.is_file()}


OnTick = Callable[[int, float, Optional[float]], Awaitable[None]]


class ProgressTracker:
    """Polls `watch_dir` for growth relative to a baseline taken at start(),
    calling `on_tick(bytes_so_far, speed_mbps, percent_or_none)` every
    POLL_INTERVAL_SECONDS until stop()."""

    def __init__(self, watch_dir: Path, estimated_total: Optional[int], on_tick: OnTick):
        self._watch_dir = watch_dir
        self._estimated_total = estimated_total
        self._on_tick = on_tick
        self._baseline: dict[Path, int] = {}
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._watch_dir.mkdir(parents=True, exist_ok=True)
        self._baseline = _snapshot(self._watch_dir)
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        prev_total = 0
        prev_time = time.monotonic()
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            current = _snapshot(self._watch_dir)
            total = sum(max(0, size - self._baseline.get(path, 0)) for path, size in current.items())

            now = time.monotonic()
            dt = now - prev_time
            speed_mbps = max(0.0, (total - prev_total) / dt / 1_000_000) if dt > 0 else 0.0

            percent = None
            if self._estimated_total and self._estimated_total > 0:
                percent = min(99.0, total / self._estimated_total * 100)

            await self._on_tick(total, speed_mbps, percent)
            prev_total = total
            prev_time = now
