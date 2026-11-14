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
            "finished_at": self.finished_at,
            "items": self.items if self.status == "done" else [],
        }


state = State()


# ---------- reading the library ----------


def _first(tags, key: str) -> str:
    v = tags.get(key) if tags else None
    return str(v[0]).strip() if v else ""


def _read(path: Path, root: Path) -> dict:
    artist = album = title = ""
    try:
        f = mutagen.File(path, easy=True)
        tags = f.tags if f is not None else None
        artist = _first(tags, "albumartist") or _first(tags, "artist")
        album = _first(tags, "album")
        title = _first(tags, "title")
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
    }


def _scan_files(root: Path) -> list[dict]:
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS and not p.name.endswith(".part"):
            out.append(_read(p, root))
    return out


# ---------- the check ----------


async def _spotify_artist(name: str, local_albums: list[str]) -> Optional[list[dict]]:
    """Discography of the Spotify artist that best matches this local artist, or None."""
    names = [name] + [n for n in _SPLIT.split(name) if n and n != name][:1]
    for n in names:
        sid = await spotify.find_artist_id(n, [{"title": a} for a in local_albums])
        if sid:
            return await spotify.discography_for(sid, DISCOGRAPHY_LIMIT)
    return None


async def _run() -> None:
    try:
        settings = config_store.load_settings()
        root = config_store.resolve_download_folder(settings)
        state.root = root
        if not spotify.available():
            raise spotify.SpotifyError("Turn on the reference catalogue under Settings > Catalogue first.")

        state.stage = "Reading your library"
        files = await asyncio.to_thread(_scan_files, root)
        state.stats["files"] = len(files)

        albums: dict[tuple[str, str], dict] = {}
        skipped = 0
        for f in files:
            if not f["artist"] or not f["album"] or spotify.norm(f["artist"]) in ("various artists", "va"):
                skipped += 1  # compilations and untagged files can't be judged
                continue
            key = (spotify.norm(f["artist"]), spotify.norm(f["album"]))
            a = albums.setdefault(key, {"artist": f["artist"], "album": f["album"], "tracks": []})
            a["tracks"].append({"path": f["path"], "title": f["title"]})
        state.stats["skipped"] = skipped
        state.stats["albums"] = len(albums)

        by_artist: dict[str, list[dict]] = {}
        for a in albums.values():
            by_artist.setdefault(spotify.norm(a["artist"]), []).append(a)

        provider = spotify.name_of(spotify.active())
        state.stage = f"Checking against {provider}"
        state.total, state.done = len(by_artist), 0
        unmatched: list[dict] = []
        for group in by_artist.values():
            disco = await _spotify_artist(group[0]["artist"], [a["album"] for a in group])
            titles = [d["title"] for d in disco if d.get("verified", True)] if disco is not None else []
            for a in group:
                if disco is None:
                    reason = f"Artist not found on {provider}"
                elif any(spotify.sim(a["album"], t) >= spotify.MATCH_THRESHOLD for t in titles):
                    state.stats["matched"] += 1
                    continue
                elif await lastfm.album_plays(a["artist"], a["album"]) >= lastfm.VOUCH_PLAYS:
                    state.stats["matched"] += 1  # Last.fm shows real listening: keep it
                    continue
                else:
                    reason = f"Not on this artist's {provider} page"
                unmatched.append({**a, "reason": reason})
            state.done += 1
            await asyncio.sleep(0.2)  # stay well under Spotify's rate limit

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


def start() -> dict:
    if state.status == "running":
        raise RuntimeError("A check is already running.")
    fresh = State()
    state.__dict__.update(fresh.__dict__)
    state.status = "running"
    state.task = asyncio.create_task(_run())
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
