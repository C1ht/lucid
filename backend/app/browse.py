"""Catalog browsing for the Search UI: search, album/artist/track/playlist
detail pages. Qobuz only -- it's the one source with rich enough, consistent
metadata (covers, artist pages, track lists) to build real browse pages from;
pasted links still work for the other sources.

Responses are normalised here into small, stable shapes so the frontend never
has to know Qobuz's raw field names. A logged-in client is kept warm between
requests (logging in costs a round trip), and results are cached briefly so
navigating back and forth is instant.
"""

import asyncio
import hashlib
import html
import re
import time
from typing import Any, Awaitable, Callable, Optional

from streamrip.client.qobuz import QobuzClient
from streamrip.config import Config

from . import config_store, paths

CACHE_TTL_SECONDS = 300
SEARCH_LIMIT = 30


class BrowseError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


# ---------- normalisation helpers ----------


def _img(image: Optional[dict], *prefs: str) -> Optional[str]:
    if not isinstance(image, dict):
        return None
    for key in prefs:
        if image.get(key):
            return image[key]
    return None


def _title(item: dict) -> str:
    title = item.get("title") or item.get("name") or "Untitled"
    version = item.get("version")
    if version and version.lower() not in title.lower() and version.lower() not in ("explicit", "clean"):
        title = f"{title} ({version})"
    return title


def _quality(bit_depth: Optional[int], rate: Optional[float]) -> Optional[str]:
    if not bit_depth or not rate:
        return None
    return f"{bit_depth}-bit/{rate:g}kHz"


def _sid(value: Any) -> Optional[str]:
    return str(value) if value not in (None, "") else None


def _strip_html(text: Optional[str]) -> str:
    if not text:
        return ""
    text = re.sub(r"<\s*br\s*/?\s*>|</p\s*>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _release_kind(tracks_count: Optional[int], duration: Optional[int]) -> str:
    """Qobuz doesn't label releases as album/EP/single in artist listings, so
    this is a heuristic on length -- the same rough cut-offs stores use."""
    n, d = tracks_count or 0, duration or 0
    if n <= 3 and d < 1500:
        return "single"
    if n <= 6 and d < 2100:
        return "ep"
    return "album"


def album_card(a: dict) -> dict:
    artist = a.get("artist") or {}
    date = a.get("release_date_original") or ""
    return {
        "id": _sid(a.get("id")),
        "title": _title(a),
        "artist": artist.get("name") or "Unknown artist",
        "artist_id": _sid(artist.get("id")),
        "cover": _img(a.get("image"), "small", "large", "thumbnail"),
        "year": date[:4],
        "date": date,
        "tracks_count": a.get("tracks_count"),
        "duration": a.get("duration"),
        "explicit": bool(a.get("parental_warning")),
        "quality": _quality(a.get("maximum_bit_depth"), a.get("maximum_sampling_rate")),
        "kind": _release_kind(a.get("tracks_count"), a.get("duration")),
    }


def artist_card(a: dict) -> dict:
    return {
        "id": _sid(a.get("id")),
        "name": a.get("name") or "Unknown artist",
        "image": _img(a.get("image"), "medium", "small", "large"),
        "albums_count": a.get("albums_count"),
    }


def track_row(t: dict, album: Optional[dict] = None) -> dict:
    album = t.get("album") or album or {}
    performer = t.get("performer") or {}
    album_artist = album.get("artist") or {}
    return {
        "id": _sid(t.get("id")),
        "title": _title(t),
        "artist": performer.get("name") or album_artist.get("name") or "",
        "artist_id": _sid(performer.get("id") or album_artist.get("id")),
        "album": album.get("title"),
        "album_id": _sid(album.get("id")),
        "cover": _img(album.get("image"), "small", "large", "thumbnail"),
        "cover_large": _img(album.get("image"), "large", "small"),
        "duration": t.get("duration"),
        "explicit": bool(t.get("parental_warning")),
        "track_number": t.get("track_number"),
        "disc": t.get("media_number") or 1,
        "quality": _quality(t.get("maximum_bit_depth"), t.get("maximum_sampling_rate")),
        "isrc": t.get("isrc"),
        "copyright": t.get("copyright") or album.get("copyright"),
        "date": t.get("release_date_original") or album.get("release_date_original"),
        "label": (album.get("label") or {}).get("name"),
        "composer": (t.get("composer") or {}).get("name"),
    }


def playlist_card(p: dict) -> dict:
    covers = p.get("images300") or p.get("images150") or p.get("images") or []
    return {
        "id": _sid(p.get("id")),
        "name": p.get("name") or "Untitled playlist",
        "owner": (p.get("owner") or {}).get("name"),
        "tracks_count": p.get("tracks_count"),
        "duration": p.get("duration"),
        "covers": covers[:4],
    }


# ---------- client + cache ----------


def _config_mtime() -> float:
    try:
        return paths.STREAMRIP_CONFIG_PATH.stat().st_mtime
    except OSError:
        return 0.0


def _friendly(exc: Exception) -> BrowseError:
    name = type(exc).__name__
    if "MissingCredentials" in name:
        return BrowseError("Qobuz isn't set up yet. Add your credentials in Settings.", 400)
    if "Authentication" in name:
        return BrowseError("Qobuz rejected your credentials. Check them in Settings.", 400)
    return BrowseError(f"Qobuz request failed: {exc}", 502)


class QobuzBrowser:
    def __init__(self) -> None:
        self._client: Optional[QobuzClient] = None
        self._client_mtime = 0.0
        self._lock = asyncio.Lock()
        self._cache: dict[str, tuple[float, Any]] = {}

    async def _drop(self) -> None:
        client, self._client = self._client, None
        session = getattr(client, "session", None)
        if session is not None:
            try:
                await session.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass

    async def _get_client(self) -> QobuzClient:
        async with self._lock:
            mtime = _config_mtime()
            if self._client is not None and mtime != self._client_mtime:
                await self._drop()  # credentials/settings changed: log in fresh
            if self._client is None:
                try:
                    try:
                        config = Config(str(paths.STREAMRIP_CONFIG_PATH))
                    except Exception as exc:  # noqa: BLE001
                        if "OutdatedConfig" not in type(exc).__name__:
                            raise
                        config_store.repair_streamrip_config()
                        config = Config(str(paths.STREAMRIP_CONFIG_PATH))
                except Exception as exc:  # noqa: BLE001
                    raise _friendly(exc) from exc
                client = QobuzClient(config)
                try:
                    await client.login()
                except Exception as exc:  # noqa: BLE001
                    session = getattr(client, "session", None)
                    if session is not None:
                        await session.close()
                    raise _friendly(exc) from exc
                self._client, self._client_mtime = client, mtime
            return self._client

    async def _with_client(self, call: Callable[[QobuzClient], Awaitable[Any]]) -> Any:
        """Run `call`, retrying once with a fresh login (covers expired sessions)."""
        for attempt in (0, 1):
            client = await self._get_client()
            try:
                return await call(client)
            except BrowseError:
                raise
            except Exception as exc:  # noqa: BLE001
                if attempt == 1:
                    raise _friendly(exc) from exc
                await self._drop()

    async def _cached(self, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
        value = await factory()
        self._cache[key] = (time.monotonic(), value)
        if len(self._cache) > 200:
            oldest = sorted(self._cache.items(), key=lambda kv: kv[1][0])[:50]
            for k, _ in oldest:
                self._cache.pop(k, None)
        return value

    async def _metadata(self, item_id: str, media_type: str) -> dict:
        async def call(client: QobuzClient) -> dict:
            try:
                return await client.get_metadata(item_id, media_type)
            except Exception as exc:  # noqa: BLE001
                if "Error fetching metadata" in str(exc):
                    raise BrowseError("That item isn't available on Qobuz.", 404) from exc
                raise

        return await self._with_client(call)

    # ---------- public API ----------

    async def search(self, query: str) -> dict:
        query = query.strip()

        async def build() -> dict:
            async def call(client: QobuzClient) -> dict:
                status, resp = await client._api_request(
                    "catalog/search", {"query": query, "limit": SEARCH_LIMIT}
                )
                if status != 200:
                    raise BrowseError(resp.get("message", "Search failed"))
                return resp

            resp = await self._with_client(call)

            def items(key: str) -> list[dict]:
                return (resp.get(key) or {}).get("items") or []

            return {
                "query": query,
                "albums": [album_card(a) for a in items("albums") if a.get("id")],
                "artists": [artist_card(a) for a in items("artists") if a.get("id")],
                "tracks": [track_row(t) for t in items("tracks") if t.get("id")],
                "playlists": [playlist_card(p) for p in items("playlists") if p.get("id")],
            }

        return await self._cached(f"search:{query.lower()}", build)

    async def preview(self, track_id: str) -> dict:
        """A short-lived streaming URL for in-browser previews. Qobuz has no
        sample intent, so this is a normal 320 kbps MP3 stream (the same thing
        the Qobuz player fetches) and the page stops it after a few seconds.
        Never cached: the URL expires."""

        async def call(client: QobuzClient) -> dict:
            fmt, ts = 5, time.time()
            sig = f"trackgetFileUrlformat_id{fmt}intentstreamtrack_id{track_id}{ts}{client.secret}"
            status, resp = await client._api_request(
                "track/getFileUrl",
                {
                    "request_ts": ts,
                    "request_sig": hashlib.md5(sig.encode("utf-8")).hexdigest(),
                    "track_id": track_id,
                    "format_id": fmt,
                    "intent": "stream",
                },
            )
            if status != 200 or not resp.get("url"):
                raise BrowseError(resp.get("message") or "No preview available for this track", 404)
            return {"url": resp["url"], "duration": resp.get("duration")}

        return await self._with_client(call)

    async def album(self, album_id: str) -> dict:
        async def build() -> dict:
            a = await self._metadata(album_id, "album")
            card = album_card(a)
            tracks = [track_row(t, a) for t in (a.get("tracks") or {}).get("items", [])]
            card.update(
                {
                    "cover_large": _img(a.get("image"), "large", "small"),
                    "genre": (a.get("genre") or {}).get("name"),
                    "label": (a.get("label") or {}).get("name"),
                    "copyright": a.get("copyright"),
                    "description": _strip_html(a.get("description")),
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
            card.update(
                {
                    "description": _strip_html(p.get("description")),
                    "tracks": [track_row(t) for t in (p.get("tracks") or {}).get("items", [])],
                }
            )
            return card

        return await self._cached(f"playlist:{playlist_id}", build)

    async def artist(self, artist_id: str, fresh: bool = False) -> dict:
        async def build() -> dict:
            a = await self._metadata(artist_id, "artist")
            bio = a.get("biography") or {}
            seen: set[str] = set()
            albums = []
            for raw in (a.get("albums") or {}).get("items", []):
                card = album_card(raw)
                if not card["id"] or card["id"] in seen:
                    continue
                seen.add(card["id"])
                card["primary"] = card["artist_id"] == artist_id
                albums.append(card)
            albums.sort(key=lambda c: c["date"] or "", reverse=True)
            return {
                "id": artist_id,
                "name": a.get("name") or "Unknown artist",
                "image": _img(a.get("image"), "large", "extralarge", "medium", "small"),
                "bio": _strip_html(bio.get("content") or bio.get("summary")),
                "albums": albums,
            }

        if fresh:
            # The monitor needs the live release list, not a copy cached up to 5 minutes ago.
            self._cache.pop(f"artist:{artist_id}", None)
        return await self._cached(f"artist:{artist_id}", build)


browser = QobuzBrowser()
