import asyncio
import json
import os
import signal
import tempfile
from collections.abc import Awaitable, Callable
from typing import Optional

from . import paths
from .schemas import DownloadByIdRequest, DownloadByUrlRequest, SearchResult

LineCallback = Callable[[str], Awaitable[None]]

# Hard ceiling on a single `rip` invocation. This is a safety net, not a tuned
# value: it exists so one stuck job (a stray interactive prompt we didn't
# anticipate, a stalled connection, a huge playlist) can never wedge the
# single-worker queue forever. Generous on purpose.
JOB_TIMEOUT_SECONDS = int(os.environ.get("LUCID_JOB_TIMEOUT_SECONDS", str(60 * 60 * 2)))


def _base_args(folder_override: Optional[str] = None) -> list[str]:
    args = [paths.RIP_BIN, "--config-path", str(paths.STREAMRIP_CONFIG_PATH), "--no-progress"]
    if folder_override is not None:
        args += ["--folder", folder_override]
    return args


async def run_streaming(args: list[str], on_line: Optional[LineCallback]) -> int:
    # stdin is explicitly closed: if a source needs credentials that aren't
    # configured, `rip` falls back to an interactive prompt instead of a clean
    # error. Without this, that prompt would hang the single-worker queue
    # forever instead of failing the job.
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(paths.MUSIC_DIR),
        start_new_session=True,  # own process group, so cancel can stop helpers like ffmpeg too
    )
    assert proc.stdout is not None

    def kill_group() -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()

    async def pump() -> int:
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").rstrip()
            if on_line is not None and line:
                await on_line(line)
        return await proc.wait()

    try:
        return await asyncio.wait_for(pump(), timeout=JOB_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        kill_group()
        await proc.wait()
        if on_line is not None:
            await on_line(f"Timed out after {JOB_TIMEOUT_SECONDS}s, killed")
        raise RuntimeError(f"rip timed out after {JOB_TIMEOUT_SECONDS}s")
    except asyncio.CancelledError:
        kill_group()
        await proc.wait()
        raise


async def download_by_id(
    req: DownloadByIdRequest, on_line: Optional[LineCallback], folder_override: Optional[str] = None
) -> int:
    args = _base_args(folder_override) + ["id", req.source, req.media_type, req.id]
    return await run_streaming(args, on_line)


async def download_by_url(
    req: DownloadByUrlRequest, on_line: Optional[LineCallback], folder_override: Optional[str] = None
) -> int:
    if "last.fm" in req.url:
        args = _base_args(folder_override) + ["lastfm", req.url]
    else:
        args = _base_args(folder_override) + ["url", req.url]
    return await run_streaming(args, on_line)


async def search(source: str, media_type: str, query: str) -> list[SearchResult]:
    fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="lucid-search-")
    os.close(fd)
    try:
        args = _base_args() + [
            "search",
            "--output-file",
            tmp_path,
            source,
            media_type,
            query,
        ]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(paths.MUSIC_DIR),
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("rip search timed out after 60s")
        if proc.returncode != 0:
            raise RuntimeError(stdout.decode(errors="replace") or "rip search failed")
        with open(tmp_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return [SearchResult.model_validate(item) for item in raw]
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
