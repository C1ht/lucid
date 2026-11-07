"""Helpers for undoing a download: streamrip's downloaded-IDs database and the
files a job placed in the library."""

import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from . import paths


def downloaded_ids() -> set[str]:
    """Track IDs streamrip has already downloaded (its own dedupe database)."""
    try:
        with sqlite3.connect(f"file:{paths.DOWNLOADS_DB_PATH}?mode=ro", uri=True) as conn:
            return {str(row[0]) for row in conn.execute("SELECT id FROM downloads")}
    except sqlite3.Error:
        return set()


def forget_ids(ids: Iterable[str]) -> None:
    """Drop IDs from streamrip's database so it will download them again."""
    ids = list(ids)
    if not ids:
        return
    try:
        with sqlite3.connect(paths.DOWNLOADS_DB_PATH) as conn:
            conn.executemany("DELETE FROM downloads WHERE id = ?", [(i,) for i in ids])
    except sqlite3.Error:
        pass


def _inside(path: str, root: str) -> Optional[Path]:
    """The resolved file path if it is a regular file strictly inside root.
    Resolving first means a symlink pointing elsewhere is refused."""
    p, r = Path(path).resolve(), Path(root).resolve()
    return p if r in p.parents else None


def file_status(files: list[str], root: str) -> dict:
    existing = sum(1 for f in files if (p := _inside(f, root)) and p.is_file())
    return {"total": len(files), "existing": existing, "missing": len(files) - existing}


def delete_files(files: list[str], root: str) -> int:
    """Delete the given files (only inside root), then remove any directories
    that are left empty, stopping at root. Returns how many files were deleted."""
    deleted = 0
    root_path = Path(root).resolve()
    parents: set[Path] = set()
    for f in files:
        p = _inside(f, root)
        if p is None or not p.is_file():
            continue
        try:
            p.unlink()
        except OSError:
            continue
        deleted += 1
        parents.add(p.parent)
    for d in sorted(parents, key=lambda x: len(x.parts), reverse=True):
        while d != root_path and root_path in d.parents:
            try:
                d.rmdir()
            except OSError:
                break
            d = d.parent
    return deleted
