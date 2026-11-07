import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from .. import paths
from .quality import new_is_lower_quality, read_quality

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".opus", ".ogg"}

MoveAction = Literal["moved", "overwritten", "skipped_lower_quality"]


@dataclass
class MoveResult:
    src: str
    dest: str
    action: MoveAction
    detail: str = ""


def staging_dir_for(job_id: str) -> Path:
    return paths.STREAMRIP_DIR / "staging" / job_id


def leftover_dir_for(job_id: str) -> Path:
    """Where a staged file goes if a move is skipped because it would have
    downgraded an existing higher-quality library file. Never auto-deleted —
    left for you to inspect and move manually if you actually want it."""
    return paths.STREAMRIP_DIR / "leftover" / job_id


def create_staging_dir(job_id: str) -> Path:
    d = staging_dir_for(job_id)
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def cleanup_staging_dir(job_id: str) -> None:
    shutil.rmtree(staging_dir_for(job_id), ignore_errors=True)


def iter_audio_files(staging_dir: Path) -> list[Path]:
    return sorted(p for p in staging_dir.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS)


def _atomic_move(src: Path, dest: Path) -> None:
    """Moves src to dest via a .part temp name in dest's own directory, then
    an atomic rename into place. Matters when src/dest end up on different
    filesystems (shutil.move falls back to copy+delete, which is NOT atomic —
    a scanner could see and index a half-written file under its real name
    mid-copy). Writing the .part name first means Navidrome never sees the
    real filename until it's a single atomic rename away from being complete."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    if tmp.exists():
        tmp.unlink()
    shutil.move(str(src), str(tmp))
    os.replace(tmp, dest)


def move_into_library(staging_dir: Path, music_dir: Path, job_id: str) -> list[MoveResult]:
    """Moves everything (audio, cover art, booklets) from staging_dir into
    music_dir, preserving the relative structure streamrip already built from
    folder_format/track_format.

    A destination audio file that already exists is only overwritten if the
    new one isn't lower quality (lossless beats lossy; otherwise compares bit
    depth*sample rate or bitrate). If the new file would be a downgrade, the
    move is skipped and the staged file is kept in leftover_dir_for(job_id)
    instead of being silently discarded. Non-audio collisions (cover art,
    booklets) are always overwritten."""
    results: list[MoveResult] = []

    for src in sorted(p for p in staging_dir.rglob("*") if p.is_file()):
        rel = src.relative_to(staging_dir)
        dest = music_dir / rel

        if not dest.exists():
            _atomic_move(src, dest)
            results.append(MoveResult(str(src), str(dest), "moved"))
            continue

        detail = ""
        if src.suffix.lower() in AUDIO_EXTS:
            existing_q = read_quality(str(dest))
            new_q = read_quality(str(src))

            if existing_q and new_q and new_is_lower_quality(existing_q, new_q):
                leftover = leftover_dir_for(job_id) / rel
                leftover.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(leftover))
                results.append(
                    MoveResult(
                        str(src), str(dest), "skipped_lower_quality",
                        f"existing {existing_q.describe()} kept over new "
                        f"{new_q.describe()}; staged file kept at {leftover}",
                    )
                )
                continue

            old_desc = existing_q.describe() if existing_q else "unknown quality"
            new_desc = new_q.describe() if new_q else "unknown quality"
            detail = f"existing {old_desc} replaced by new {new_desc}"
        else:
            detail = "non-audio file replaced"

        dest.unlink()
        _atomic_move(src, dest)
        results.append(MoveResult(str(src), str(dest), "overwritten", detail))

    return results
