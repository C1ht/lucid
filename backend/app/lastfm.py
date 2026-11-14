"""Last.fm as a second opinion on what an artist really released.

Optional (a free API key). Lookups use the MusicBrainz artist id, so
same-name artists are never mixed up. Last.fm is built from what people
actually listen to, so it knows small releases MusicBrainz is missing, and
AI filler uploaded to a store has next to no plays.
"""

import time
from typing import Optional

import httpx

from . import config_store
from .catalog_base import norm

API = "https://ws.audioscrobbler.com/2.0/"
VOUCH_PLAYS = 50  # plays before Last.fm counts as vouching for a release
_cache: dict[str, tuple[float, object]] = {}


def key() -> str:
    return config_store.load_settings().catalog.lastfm_key.strip()


def available() -> bool:
    return bool(key())


async def _call(method: str, **params) -> Optional[dict]:
    ck = f"{method}:{sorted(params.items())}"
    hit = _cache.get(ck)
    if hit and time.monotonic() - hit[0] < 3600:
        return hit[1]  # type: ignore[return-value]
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.get(API, params={"method": method, "api_key": key(), "format": "json", **params})
        data = r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        data = None
    if data is not None and "error" in data:
        data = None  # not found / bad key: no opinion
    _cache[ck] = (time.monotonic(), data)
    return data


async def top_albums(mbid: str) -> dict[str, tuple[str, int]]:
    """normalised title -> (title, play count), for the artist with this MusicBrainz id."""
    if not available():
        return {}
    data = await _call("artist.gettopalbums", mbid=mbid, limit=100)
    out: dict[str, tuple[str, int]] = {}
    for a in ((data or {}).get("topalbums") or {}).get("album") or []:
        try:
            out[norm(a.get("name"))] = (a.get("name") or "", int(a.get("playcount") or 0))
        except ValueError:
            pass
    out.pop("", None)
    return out


async def artist_bio(mbid: str) -> tuple[str, Optional[int]]:
    """(plain-text bio, listeners) for the artist with this MusicBrainz id."""
    if not available():
        return "", None
    data = await _call("artist.getinfo", mbid=mbid)
    a = (data or {}).get("artist") or {}
    bio = ((a.get("bio") or {}).get("summary") or "").split("<a href")[0].strip()
    try:
        listeners = int((a.get("stats") or {}).get("listeners") or 0) or None
    except ValueError:
        listeners = None
    return bio, listeners


async def album_plays(artist: str, album: str) -> int:
    """Plays Last.fm has for this album by this artist name (0 if unknown). Name-based,
    so it is only used to *keep* things in the library check, never to delete."""
    if not available():
        return 0
    data = await _call("album.getinfo", artist=artist, album=album)
    try:
        return int(((data or {}).get("album") or {}).get("playcount") or 0)
    except ValueError:
        return 0
