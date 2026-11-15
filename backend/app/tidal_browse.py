"""Tidal as a download source: the same browsing interface as the Qobuz one (search, album,
artist, track, playlist pages and previews), returning the same small shapes so the rest of Lucid
doesn't care which source it is talking to. Built on streamrip's own Tidal client, so it uses the
login the downloads use."""

import asyncio
import base64
import json
from typing import Any, Optional

from streamrip.client.tidal import TidalClient

from .browse import SEARCH_LIMIT, BrowseError, QobuzBrowser, _release_kind, _sid, _title


def _image(uuid: Optional[str], size: int) -> Optional[str]:
    if not uuid:
        return None
    return f"https://resources.tidal.com/images/{str(uuid).replace('-', '/')}/{size}x{size}.jpg"


def _quality(item: dict) -> Optional[str]:
    tags = ((item.get("mediaMetadata") or {}).get("tags")) or []
    q = item.get("audioQuality") or ""
    if "HIRES_LOSSLESS" in tags or q in ("HI_RES", "HI_RES_LOSSLESS"):
        return "hi-res FLAC"
    if q == "LOSSLESS" or "LOSSLESS" in tags:
        return "16-bit/44.1kHz"
    if q in ("HIGH", "LOW"):
        return "lossy (AAC)"
    return None


def _artists(item: dict) -> tuple[str, Optional[str], list[str]]:
    """(credit, main artist id, main artist ids) for an album or track."""
    people = item.get("artists") or ([item["artist"]] if item.get("artist") else [])
    main = [a for a in people if (a.get("type") or "MAIN") == "MAIN"] or people
    names = [a.get("name") for a in main if a.get("name")]
    lead = (item.get("artist") or (main[0] if main else {})).get("id")
    return ", ".join(names) or "Unknown artist", _sid(lead), [str(a["id"]) for a in main if a.get("id") is not None]


_KIND = {"ALBUM": "album", "EP": "ep", "SINGLE": "single"}


def album_card(a: dict) -> dict:
    credit, lead, _ = _artists(a)
    date = a.get("releaseDate") or (a.get("streamStartDate") or "")[:10]
    n = a.get("numberOfTracks")
    return {
        "id": _sid(a.get("id")),
        "title": _title(a),
        "artist": credit,
        "artist_id": lead,
        "cover": _image(a.get("cover"), 320),
        "cover_large": _image(a.get("cover"), 1280),
        "genre": None,
        "year": date[:4],
        "date": date,
        "tracks_count": n,
        "duration": a.get("duration"),
        "explicit": bool(a.get("explicit")),
        "quality": _quality(a),
        "kind": _KIND.get(a.get("type") or "", _release_kind(n, a.get("duration"))),
    }


def artist_card(a: dict) -> dict:
    return {"id": _sid(a.get("id")), "name": a.get("name") or "Unknown artist", "image": _image(a.get("picture"), 480), "albums_count": None}


def track_row(t: dict, album: Optional[dict] = None) -> dict:
    alb = t.get("album") or album or {}
    credit, lead, _ = _artists(t)
    return {
        "id": _sid(t.get("id")),
        "title": _title(t),
        "artist": credit,
        "artist_id": lead,
        "album": alb.get("title"),
        "album_id": _sid(alb.get("id")),
        "cover": _image(alb.get("cover"), 320),
        "cover_large": _image(alb.get("cover"), 1280),
        "duration": t.get("duration"),
        "explicit": bool(t.get("explicit")),
        "track_number": t.get("trackNumber"),
        "disc": t.get("volumeNumber") or 1,
        "quality": _quality(t),
        "isrc": t.get("isrc"),
        "copyright": t.get("copyright") or (album or {}).get("copyright"),
        "date": (album or alb).get("releaseDate") or (t.get("streamStartDate") or "")[:10],
        "label": None,
        "composer": None,
    }


def playlist_card(p: dict) -> dict:
    return {
        "id": _sid(p.get("uuid") or p.get("id")),
        "name": p.get("title") or "Untitled playlist",
        "owner": (p.get("creator") or {}).get("name"),
        "tracks_count": p.get("numberOfTracks"),
        "duration": p.get("duration"),
        "covers": [c for c in [_image(p.get("squareImage") or p.get("image"), 320)] if c],
    }


class TidalBrowser(QobuzBrowser):
    label = "Tidal"
    client_cls = TidalClient

    async def _items(self, kind: str, query: str) -> list[dict]:
        async def call(client: TidalClient) -> list[dict]:
            resp = await client._api_request(f"search/{kind}", {"query": query})
            return (resp or {}).get("items") or []

        return await self._with_client(call)

    async def search(self, query: str) -> dict:
        query = query.strip()

        async def build() -> dict:
            albums, artists, tracks = await asyncio.gather(
                self._items("albums", query), self._items("artists", query), self._items("tracks", query)
            )
            return {
                "query": query,
                "albums": [album_card(a) for a in albums[:SEARCH_LIMIT] if a.get("id")],
                "artists": [artist_card(a) for a in artists[:SEARCH_LIMIT] if a.get("id")],
                "tracks": [track_row(t) for t in tracks[:SEARCH_LIMIT] if t.get("id")],
                "playlists": [],
            }

        return await self._cached(f"search:{query.lower()}", build)

    async def preview(self, track_id: str) -> dict:
        """A short-lived stream URL (320 kbps AAC) for in-browser previews. Never cached."""

        async def call(client: TidalClient) -> dict:
            resp = await client._api_request(
                f"tracks/{track_id}/playbackinfopostpaywall",
                {"audioquality": "HIGH", "playbackmode": "STREAM", "assetpresentation": "FULL"},
            )
            try:
                manifest = json.loads(base64.b64decode(resp["manifest"]).decode("utf-8"))
            except (KeyError, ValueError):
                raise BrowseError(resp.get("userMessage") or "No preview available for this track", 404)
            if (manifest.get("encryptionType") or "NONE") != "NONE" or not manifest.get("urls"):
                raise BrowseError("No preview available for this track", 404)
            return {"url": manifest["urls"][0], "duration": None}

        return await self._with_client(call)

    async def album(self, album_id: str) -> dict:
        async def build() -> dict:
            a = await self._metadata(album_id, "album")
            card = album_card(a)
            tracks = [track_row(t, a) for t in a.get("tracks") or []]
            card.update(
                {
                    "copyright": a.get("copyright"),
                    "description": "",
                    "label": None,
                    "tracks": tracks,
                    "disc_count": max([t["disc"] for t in tracks] or [1]),
                }
            )
            return card

        return await self._cached(f"album:{album_id}", build)

    async def track(self, track_id: str) -> dict:
        async def build() -> dict:
            return track_row(await self._metadata(track_id, "track"))

        return await self._cached(f"track:{track_id}", build)

    async def playlist(self, playlist_id: str) -> dict:
        async def build() -> dict:
            p = await self._metadata(playlist_id, "playlist")
            card = playlist_card(p)
            card.update({"description": p.get("description") or "", "tracks": [track_row(t) for t in p.get("tracks") or []]})
            return card

        return await self._cached(f"playlist:{playlist_id}", build)

    async def artist(self, artist_id: str, fresh: bool = False) -> dict:
        async def build() -> dict:
            a: Any = await self._metadata(artist_id, "artist")
            seen: set[str] = set()
            albums = []
            for raw in a.get("albums") or []:
                card = album_card(raw)
                if not card["id"] or card["id"] in seen:
                    continue
                seen.add(card["id"])
                _, _, mains = _artists(raw)
                card["primary"] = str(artist_id) in mains
                albums.append(card)
            albums.sort(key=lambda c: c["date"] or "", reverse=True)
            return {
                "id": artist_id,
                "name": a.get("name") or "Unknown artist",
                "image": _image(a.get("picture"), 750),
                "bio": "",
                "albums": albums,
            }

        if fresh:
            self._cache.pop(f"artist:{artist_id}", None)
        return await self._cached(f"artist:{artist_id}", build)


browser = TidalBrowser()
