"""Library check: compare what is already downloaded with Spotify.

Every audio file under the download folder is grouped into albums by its tags.
An album is "matched" when its artist exists on Spotify and the artist's
Spotify discography lists a release with that title. Everything else is
reported so the user can pick what to delete. Nothing is deleted by the scan;
deletion only ever touches files the last scan reported.
"""

import asyncio
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import mutagen

from . import config_store, lastfm, library, navidrome, spotify
from .browse import BrowseError
from .tagging.staging import AUDIO_EXTS

DISCOGRAPHY_LIMIT = 300  # a scan needs the whole catalogue, not the first page
_SPLIT = re.compile(r"\s*(?:,|;|/| & | feat\.? | ft\.? | x )\s*", re.I)
_JUNK = {".jpg", ".jpeg", ".png", ".webp"}  # cover art left behind in emptied folders


class State:
    def __init__(self) -> None:
        self.status = "idle"  # idle | running | done | error
        self.error: Optional[str] = None
        self.stage = ""
        self.done = 0
        self.total = 0
        self.root: Optional[Path] = None
        self.scope: Optional[str] = None  # an artist name when only one artist is being checked
        self.items: list[dict] = []  # unmatched albums
        self.stats = {"files": 0, "albums": 0, "matched": 0, "unmatched": 0, "skipped": 0}
        self.finished_at: Optional[float] = None
        self.task: Optional[asyncio.Task] = None

    def public(self) -> dict:
        return {
            "status": self.status,
            "error": self.error,
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "stats": self.stats,
            "scope": self.scope,
            "finished_at": self.finished_at,
            "items": self.items if self.status == "done" else [],
        }


state = State()


# ---------- reading the library ----------


def _first(tags, key: str) -> str:
    v = tags.get(key) if tags else None
    return str(v[0]).strip() if v else ""


def _read(path: Path, root: Path) -> dict:
    artist = album = title = year = ""
    try:
        f = mutagen.File(path, easy=True)
        tags = f.tags if f is not None else None
        artist = _first(tags, "albumartist") or _first(tags, "artist")
        album = _first(tags, "album")
        title = _first(tags, "title")
        year = _first(tags, "date")[:4]
    except Exception:  # unreadable tags: fall back to the folder layout
        pass
    parts = path.relative_to(root).parts
    if not artist and len(parts) >= 3:
        artist = parts[-3]
    if not album and len(parts) >= 2:
        album = parts[-2]
    return {
        "path": str(path.relative_to(root)),
        "artist": artist,
        "album": album,
        "title": title or path.stem,
        "year": year,
    }


def _credits(field: str) -> list[str]:
    """Normalised names in a credit: "A, B" -> [a, b]. The whole credit is included."""
    return [spotify.norm(field)] + [spotify.norm(n) for n in _SPLIT.split(field) if n]


def _is_artist(field: str, name: str, loose: bool) -> bool:
    """Is `field` (a file's artist tag) this artist? Strictly the whole credit; loosely any
    name in a collaboration credit too."""
    want = spotify.norm(name)
    if not want:
        return False
    return want in _credits(field) if loose else spotify.norm(field) == want


def _scan_files(root: Path, only: Optional[str] = None) -> list[dict]:
    paths = [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() in AUDIO_EXTS and not p.name.endswith(".part")]
    if only:
        # Folders are named after the artist, so skip reading tags elsewhere. If nothing is
        # laid out that way, fall back to reading everything.
        near = [p for p in paths if len(p.relative_to(root).parts) >= 2 and spotify.norm(only) in _credits(p.relative_to(root).parts[0])]
        paths = near or paths
    # Reading tags is I/O bound, so do it in parallel.
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda p: _read(p, root), paths))
    return [r for r in rows if _is_artist(r["artist"], only, True)] if only else rows


# ---------- the check ----------


async def _check_artist(name: str, albums: list[str], years: Optional[list[str]] = None) -> tuple[bool, list[bool]]:
    """Known + per-album verdicts, trying the first name of a "A, B" credit if the whole isn't known."""
    names = [name] + [n for n in _SPLIT.split(name) if n and n != name][:1]
    result: tuple[bool, list[bool]] = (False, [False] * len(albums))
    for n in names:
        result = await spotify.library_match(n, albums, years)
        if result[0]:
            break
    return result


async def _run(only: Optional[str] = None) -> None:
    try:
        settings = config_store.load_settings()
        root = config_store.resolve_download_folder(settings)
        state.root = root
        if not spotify.available():
            raise spotify.SpotifyError("Turn on the reference catalogue under Settings > Catalogue first.")

        state.stage = "Reading your library"
        files = await asyncio.to_thread(_scan_files, root, only)
        state.stats["files"] = len(files)

        albums: dict[tuple[str, str], dict] = {}
        skipped = 0
        for f in files:
            if not f["artist"] or not f["album"] or spotify.norm(f["artist"]) in ("various artists", "va"):
                skipped += 1  # compilations and untagged files can't be judged
                continue
            key = (spotify.norm(f["artist"]), spotify.norm(f["album"]))
            a = albums.setdefault(key, {"artist": f["artist"], "album": f["album"], "year": "", "tracks": []})
            a["year"] = a["year"] or f.get("year", "")
            a["tracks"].append({"path": f["path"], "title": f["title"]})
        state.stats["skipped"] = skipped
        state.stats["albums"] = len(albums)

        by_artist: dict[str, list[dict]] = {}
        for a in albums.values():
            by_artist.setdefault(spotify.norm(a["artist"]), []).append(a)

        provider = spotify.name_of(spotify.active())
        if provider == "MusicBrainz" and settings.catalog.include_deezer:
            provider = "MusicBrainz/Deezer"
        state.stage = f"Checking against {provider}"
        state.total, state.done = len(by_artist), 0
        unmatched: list[dict] = []
        state.stats["unchecked"] = 0
        gate = asyncio.Semaphore(8)  # Deezer runs in parallel; MusicBrainz queues itself at 1 request/s

        async def check(group: list[dict]) -> None:
            async with gate:
                try:
                    known, ok = await _check_artist(group[0]["artist"], [a["album"] for a in group], [a["year"] for a in group])
                except (spotify.SpotifyError, BrowseError):
                    # A source was unreachable. Say nothing about this artist rather than
                    # calling its albums fake and offering them for deletion.
                    state.stats["unchecked"] += 1
                    state.done += 1
                    return
            for a, good in zip(group, ok):
                if good:
                    state.stats["matched"] += 1
                else:
                    unmatched.append({**a, "reason": f"Artist not found on {provider}" if not known else f"Not on this artist's {provider} page"})
            state.done += 1

        await asyncio.gather(*[check(g) for g in by_artist.values()])

        unmatched.sort(key=lambda a: (a["artist"].lower(), a["album"].lower()))
        for i, a in enumerate(unmatched):
            a["id"] = i
        state.items = unmatched
        state.stats["unmatched"] = len(unmatched)
        state.status, state.stage = "done", ""
        state.finished_at = time.time()
    except (spotify.SpotifyError, BrowseError, ValueError, OSError) as exc:
        state.status, state.error, state.stage = "error", str(exc), ""
    except Exception as exc:  # noqa: BLE001 - never leave the UI stuck on "running"
        state.status, state.error, state.stage = "error", f"Unexpected error: {exc}", ""


def start(artist: Optional[str] = None) -> dict:
    """Check the whole library, or only `artist`'s albums."""
    if state.status == "running":
        raise RuntimeError("A check is already running.")
    fresh = State()
    state.__dict__.update(fresh.__dict__)
    state.status = "running"
    state.scope = artist or None
    state.task = asyncio.create_task(_run(artist or None))
    return state.public()


# ---------- deleting what the scan reported ----------


def _tidy(dirs: set[Path], root: Path) -> None:
    """Remove folders left without audio, including their cover images."""
    for d in sorted(dirs, key=lambda x: len(x.parts), reverse=True):
        while d != root and root in d.parents:
            try:
                left = list(d.iterdir())
            except OSError:
                break
            if any(p.is_dir() or p.suffix.lower() in AUDIO_EXTS for p in left):
                break
            for p in left:
                if p.suffix.lower() in _JUNK:
                    p.unlink(missing_ok=True)
            try:
                d.rmdir()
            except OSError:
                break
            d = d.parent


async def delete(paths: Optional[list[str]]) -> dict:
    """Delete the given files (None = everything the scan reported). Only paths
    from the last scan are accepted."""
    if state.status != "done" or state.root is None:
        raise ValueError("Run a check first.")
    root = state.root.resolve()
    allowed = {t["path"] for a in state.items for t in a["tracks"]}
    wanted = allowed if paths is None else {p for p in paths if p in allowed}
    parents: set[Path] = set()
    for rel in wanted:
        p = (root / rel).resolve()
        if root in p.parents:
            parents.add(p.parent)
    deleted = library.delete_files([str(root / rel) for rel in wanted], str(root))
    _tidy(parents, root)

    for a in state.items:
        a["tracks"] = [t for t in a["tracks"] if t["path"] not in wanted]
    state.items = [a for a in state.items if a["tracks"]]
    state.stats["unmatched"] = len(state.items)
    if deleted:
        await navidrome.trigger_scan(config_store.load_settings().navidrome)
    return {"deleted": deleted, "remaining": len(state.items)}


# ---------- deleting everything by one artist ----------


def _artist_albums(name: str) -> tuple[Path, list[dict]]:
    root = config_store.resolve_download_folder(config_store.load_settings()).resolve()
    rows = [r for r in _scan_files(root, name) if _is_artist(r["artist"], name, False)]
    return root, rows


async def artist_summary(name: str) -> dict:
    _, rows = await asyncio.to_thread(_artist_albums, name)
    return {"files": len(rows), "albums": len({spotify.norm(r["album"]) for r in rows})}


async def delete_artist(name: str) -> dict:
    """Delete every audio file credited to this artist (the whole credit, so a collaboration
    filed under someone else is left alone), then their emptied folders."""
    if not spotify.norm(name):
        raise ValueError("Which artist?")
    root, rows = await asyncio.to_thread(_artist_albums, name)
    parents = {(root / r["path"]).resolve().parent for r in rows}
    deleted = library.delete_files([str(root / r["path"]) for r in rows], str(root))
    _tidy(parents, root)
    if deleted:
        await navidrome.trigger_scan(config_store.load_settings().navidrome)
    # A finished scan may list files that are gone now.
    gone = {r["path"] for r in rows}
    for a in state.items:
        a["tracks"] = [t for t in a["tracks"] if t["path"] not in gone]
    state.items = [a for a in state.items if a["tracks"]]
    return {"deleted": deleted}
