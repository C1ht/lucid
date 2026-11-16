"""Deezer as a download source: the same browsing interface as the Qobuz and Tidal browsers (search,
album, artist, track, playlist pages and previews) built on Deezer's public API, which needs no
login. Only the actual download uses your Deezer account (the ARL cookie in Settings), through
streamrip."""

import asyncio
from typing import Optional

from . import deezer
from .browse import BrowseError, _sid, _title
from .catalog_base import CatalogError

_KIND = {"album": "album", "ep": "ep", "single": "single", "compile": "album"}


def _credit(item: dict) -> tuple[str, Optional[str]]:
    """(artist credit, main artist id) from an album/track's contributors, else its artist."""
    people = item.get("contributors") or []
    main = [p for p in people if (p.get("role") or "Main") == "Main"] or people
    names = [p["name"] for p in main if p.get("name")]
    artist = item.get("artist") or (main[0] if main else {})
    return ", ".join(names) or artist.get("name") or "Unknown artist", _sid(artist.get("id"))


def _titled(item: dict) -> dict:
    version = item.get("title_version") or item.get("version")
    return {"title": item.get("title") or item.get("title_short") or "Untitled", "version": version}


def album_card(a: dict) -> dict:
    credit, lead = _credit(a)
    date = a.get("release_date") or ""
    genres = ((a.get("genres") or {}).get("data")) or []
    return {
        "id": _sid(a.get("id")),
        "title": _title(_titled(a)),
        "artist": credit,
        "artist_id": lead,
        "cover": a.get("cover_medium") or a.get("cover_big"),
        "cover_large": a.get("cover_xl") or a.get("cover_big"),
        "genre": genres[0]["name"] if genres and genres[0].get("name") else None,
        "year": date[:4],
        "date": date,
        "tracks_count": a.get("nb_tracks"),
        "duration": a.get("duration"),
        "explicit": bool(a.get("explicit_lyrics")),
        "quality": "lossless FLAC",
        "kind": _KIND.get(a.get("record_type") or "album", "album"),
    }


def artist_card(a: dict) -> dict:
    pic = a.get("picture_medium") or a.get("picture_big")
    return {
        "id": _sid(a.get("id")),
        "name": a.get("name") or "Unknown artist",
        "image": pic if pic and "/images/artist//" not in pic else None,
        "albums_count": a.get("nb_album"),
    }


def track_row(t: dict, album: Optional[dict] = None) -> dict:
    alb = t.get("album") or album or {}
    credit, lead = _credit(t)
    return {
        "id": _sid(t.get("id")),
        "title": _title(_titled(t)),
        "artist": credit,
        "artist_id": lead,
        "album": alb.get("title"),
        "album_id": _sid(alb.get("id")),
        "cover": alb.get("cover_medium"),
        "cover_large": alb.get("cover_xl"),
        "duration": t.get("duration"),
        "explicit": bool(t.get("explicit_lyrics")),
        "track_number": t.get("track_position"),
        "disc": t.get("disk_number") or 1,
        "quality": "lossless FLAC",
        "isrc": t.get("isrc"),
        "copyright": None,
        "date": t.get("release_date") or (album or alb).get("release_date"),
        "label": (album or alb).get("label"),
        "composer": None,
    }


def playlist_card(p: dict) -> dict:
    return {
        "id": _sid(p.get("id")),
        "name": p.get("title") or "Untitled playlist",
        "owner": (p.get("creator") or {}).get("name"),
        "tracks_count": p.get("nb_tracks"),
        "duration": p.get("duration"),
        "covers": [c for c in [p.get("picture_medium")] if c],
    }


async def _get(path: str, params: Optional[dict] = None) -> dict:
    try:
        return await deezer.client.get(path, params)
    except CatalogError as exc:
        raise BrowseError(str(exc), 404 if "didn't have" in str(exc) else 502) from exc


class DeezerBrowser:
    label = "Deezer"

    async def search(self, query: str) -> dict:
        query = query.strip()
        albums, artists, tracks = await asyncio.gather(
            _get("/search/album", {"q": query, "limit": 30}),
            _get("/search/artist", {"q": query, "limit": 30}),
            _get("/search/track", {"q": query, "limit": 30}),
        )
        return {
            "query": query,
            "albums": [album_card(a) for a in albums.get("data") or [] if a.get("id")],
            "artists": [artist_card(a) for a in artists.get("data") or [] if a.get("id")],
            "tracks": [track_row(t) for t in tracks.get("data") or [] if t.get("id")],
            "playlists": [],
        }

    async def suggest(self, kind: str, query: str, artist: str = "") -> list[dict]:
        found = await self.search(f"{artist} {query}".strip() if kind == "album" else query)
        if kind == "artist":
            return [{"name": a["name"], "image": a["image"]} for a in found["artists"][:6]]
        mine = [a for a in found["albums"] if not artist or artist.lower() in (a["artist"] or "").lower()] or found["albums"]
        return [{"title": a["title"], "artist": a["artist"], "year": a["year"], "id": a["id"]} for a in mine[:6]]

    async def preview(self, track_id: str) -> dict:
        """Deezer's own 30-second preview clip."""
        deezer.client.forget(f"/track/{track_id}")  # the clip's link is signed and expires
        t = await _get(f"/track/{track_id}")
        if not t.get("preview"):
            raise BrowseError("No preview available for this track", 404)
        return {"url": t["preview"], "duration": 30}

    async def album(self, album_id: str) -> dict:
        a = await _get(f"/album/{album_id}")
        rows = await deezer.client._paged(f"/album/{album_id}/tracks", 300)
        card = album_card(a)
        tracks = [track_row(t, a) for t in rows]
        card.update(
            {
                "label": a.get("label"),
                "copyright": None,
                "description": "",
                "tracks": tracks,
                "disc_count": max([t["disc"] for t in tracks] or [1]),
            }
        )
        return card

    async def track(self, track_id: str) -> dict:
        return track_row(await _get(f"/track/{track_id}"))

    async def playlist(self, playlist_id: str) -> dict:
        p = await _get(f"/playlist/{playlist_id}")
        card = playlist_card(p)
        rows = await deezer.client._paged(f"/playlist/{playlist_id}/tracks", 300)
        card.update({"description": p.get("description") or "", "tracks": [track_row(t) for t in rows]})
        return card

    async def artist(self, artist_id: str, fresh: bool = False) -> dict:
        if fresh:
            deezer.client.forget(f"/artist/{artist_id}/albums")
        a = await _get(f"/artist/{artist_id}")
        try:
            rows = await deezer.client._paged(f"/artist/{artist_id}/albums", 300)
        except CatalogError as exc:
            raise BrowseError(str(exc), 502) from exc
        seen: set[str] = set()
        albums = []
        for raw in rows:
            card = album_card({**raw, "artist": raw.get("artist") or a})
            if not card["id"] or card["id"] in seen:
                continue
            seen.add(card["id"])
            card["artist"], card["artist_id"] = a.get("name") or card["artist"], str(artist_id)
            card["primary"] = True  # Deezer's artist page only lists releases credited to the artist
            albums.append(card)
        albums.sort(key=lambda c: c["date"] or "", reverse=True)
        return {"id": str(artist_id), "name": a.get("name") or "Unknown artist", "image": artist_card(a)["image"], "bio": "", "albums": albums}


browser = DeezerBrowser()


async def full_album(album_id: str) -> dict:
    """An album with each track's own details (contributors, ISRC), for tagging."""
    a = await _get(f"/album/{album_id}")
    rows = await deezer.client._paged(f"/album/{album_id}/tracks", 300)
    sem = asyncio.Semaphore(4)

    async def one(t: dict) -> dict:
        async with sem:
            try:
                return await _get(f"/track/{t['id']}")
            except BrowseError:
                return t  # the basic row still has the numbers

    a["tracks"] = list(await asyncio.gather(*[one(t) for t in rows]))
    return a
