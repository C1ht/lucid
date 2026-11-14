"""Reference catalogues (MusicBrainz, optionally Spotify) for search and browsing --
nothing is ever downloaded from them. (The module keeps its old name.)

Qobuz's catalogue contains plenty of releases filed under the wrong artist
(same-name uploads, AI filler). Spotify's artist pages are curated, so we ask
Spotify what an artist really released and then look each of those releases up
on Qobuz. Only the releases that exist on both are shown, and the Qobuz ids
that come back are what the download pipeline uses as before.

Auth is the app-only "client credentials" flow (a free Spotify developer app:
client id + secret). No user login, no playback, no library access.
"""

import asyncio
import base64
import difflib
import re
import time
import unicodedata
from typing import Any, Optional

import httpx

from . import config_store, deezer, lastfm, musicbrainz
from .browse import BrowseError, browser
from .catalog_base import MATCH_THRESHOLD, CatalogError, norm, sim

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
CACHE_TTL = 600
# Search results are small in recent Spotify API versions; page through
# anything longer with `next` instead of relying on a large `limit`.
PAGE = 10
MAX_DISCOGRAPHY = 40
DISCOGRAPHY_ALL = 300


SpotifyError = CatalogError  # one error type for every catalogue


# ---------- name matching ----------


def _artist_ok(qobuz_name: str, spotify_names: list[str]) -> bool:
    q = norm(qobuz_name)
    return any(q == norm(n) or sim(q, n) >= 0.92 for n in spotify_names)


# ---------- API client ----------


class SpotifyClient:
    def __init__(self) -> None:
        self._token: Optional[tuple[str, float, str]] = None  # token, expiry, credentials key
        self._lock = asyncio.Lock()
        self._cache: dict[str, tuple[float, Any]] = {}

    @staticmethod
    def credentials() -> tuple[str, str]:
        s = config_store.load_settings().spotify
        if not (s.client_id.strip() and s.client_secret.strip()):
            raise SpotifyError("Add your Spotify client ID and secret under Settings > Spotify.")
        return s.client_id.strip(), s.client_secret.strip()

    async def _auth(self, force: bool = False) -> str:
        cid, secret = self.credentials()
        key = f"{cid}:{secret}"
        async with self._lock:
            t = self._token
            if not force and t and t[2] == key and t[1] > time.monotonic() + 30:
                return t[0]
            basic = base64.b64encode(key.encode()).decode()
            try:
                async with httpx.AsyncClient(timeout=15) as http:
                    r = await http.post(
                        TOKEN_URL,
                        data={"grant_type": "client_credentials"},
                        headers={"Authorization": f"Basic {basic}"},
                    )
            except httpx.HTTPError as exc:
                raise SpotifyError(f"Couldn't reach Spotify ({type(exc).__name__}).") from exc
            if r.status_code in (400, 401):
                raise SpotifyError("Spotify rejected the client ID/secret. Check them under Settings > Spotify.")
            if r.status_code != 200:
                raise SpotifyError(f"Spotify login failed ({r.status_code}).")
            body = r.json()
            self._token = (body["access_token"], time.monotonic() + int(body.get("expires_in", 3600)), key)
            return self._token[0]

    async def get(self, path_or_url: str, params: Optional[dict] = None) -> dict:
        url = path_or_url if path_or_url.startswith("http") else f"{API}{path_or_url}"
        for attempt in range(2):
            token = await self._auth(force=attempt == 1)
            try:
                async with httpx.AsyncClient(timeout=15) as http:
                    r = await http.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as exc:
                raise SpotifyError(f"Couldn't reach Spotify ({type(exc).__name__}).") from exc
            if r.status_code == 401 and attempt == 0:
                continue
            if r.status_code == 429:
                wait = r.headers.get("Retry-After", "a bit")
                raise SpotifyError(f"Spotify is rate limiting this app. Try again in {wait}s.")
            if r.status_code == 403:
                try:
                    said = (r.json().get("error") or {}).get("message") or ""
                except ValueError:
                    said = r.text[:120]
                where = url.replace(API, "").split("?")[0]
                raise SpotifyError(
                    f"Spotify refused {where} (403: {said or 'no reason given'}). "
                    "If it mentions Premium, the account that owns the app on developer.spotify.com "
                    "must have Premium (it can take a while to apply); also check the app is the one "
                    "whose Client ID/secret you pasted."
                )
            if r.status_code != 200:
                raise SpotifyError(f"Spotify error {r.status_code} on {url.replace(API, '').split('?')[0]}.")
            return r.json()
        raise SpotifyError("Spotify login failed.")

    async def cached(self, key: str, factory):
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        value = await factory()
        self._cache[key] = (time.monotonic(), value)
        if len(self._cache) > 200:
            for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[:50]:
                self._cache.pop(k, None)
        return value

    # ---- catalogue calls ----

    async def search(self, query: str) -> dict:
        async def build() -> dict:
            resp = await self.get(
                "/search", {"q": query, "type": "artist,album,track", "limit": PAGE}
            )
            return {
                "artists": [
                    {
                        "id": a["id"],
                        "name": a["name"],
                        "popularity": a.get("popularity", 0),
                        "image": _image(a),
                        "p": "sp:",
                    }
                    for a in (resp.get("artists") or {}).get("items", [])
                    if a
                ],
                "albums": [self._album(a) for a in (resp.get("albums") or {}).get("items", []) if a],
                "tracks": [self._track(t) for t in (resp.get("tracks") or {}).get("items", []) if t],
            }

        return await self.cached(f"search:{query.lower()}", build)

    async def artist(self, artist_id: str) -> dict:
        async def build() -> dict:
            a = await self.get(f"/artists/{artist_id}")
            return {
                "name": a.get("name") or "",
                "image": _image(a, big=True),
                "genres": a.get("genres") or [],
                "followers": (a.get("followers") or {}).get("total"),
            }

        return await self.cached(f"artist:{artist_id}", build)

    async def discography(self, artist_id: str, limit: int = MAX_DISCOGRAPHY) -> list[dict]:
        async def build() -> list[dict]:
            out: list[dict] = []
            seen: set[str] = set()
            url: Optional[str] = f"/artists/{artist_id}/albums"
            params: Optional[dict] = {"include_groups": "album,single", "limit": PAGE}
            while url and len(out) < limit:
                resp = await self.get(url, params)
                for a in resp.get("items") or []:
                    if a and a["id"] not in seen:
                        seen.add(a["id"])
                        out.append(self._album(a))
                url, params = resp.get("next"), None
            return out

        return await self.cached(f"disco:{artist_id}:{limit}", build)

    @staticmethod
    def _album(a: dict) -> dict:
        arts = a.get("artists") or []
        return {
            "p": "sp:",
            "id": a.get("id"),
            "title": a.get("name") or "",
            "artists": [x.get("name") for x in arts if x.get("name")],
            "artist_ids": [x.get("id") for x in arts if x.get("id")],
            "tracks": a.get("total_tracks"),
            "year": (a.get("release_date") or "")[:4],
            "date": a.get("release_date") or "",
            "cover": _image(a, big=False),
            "cover_large": _image(a, big=True),
            "group": a.get("album_group") or a.get("album_type") or "album",
            "label": a.get("label"),
            "copyright": "; ".join(c.get("text", "") for c in (a.get("copyrights") or [])[:1]),
        }

    @staticmethod
    def _track(t: dict) -> dict:
        album = t.get("album") or {}
        arts = t.get("artists") or []
        return {
            "p": "sp:",
            "id": t.get("id"),
            "title": t.get("name") or "",
            "artists": [x.get("name") for x in arts if x.get("name")],
            "artist_ids": [x.get("id") for x in arts if x.get("id")],
            "album": album.get("name") or "",
            "album_id": album.get("id"),
            "cover": _image(album, big=False),
            "cover_large": _image(album, big=True),
            "date": album.get("release_date") or "",
            "duration": (t.get("duration_ms") or 0) / 1000,
            "explicit": bool(t.get("explicit")),
            "track_number": t.get("track_number"),
            "disc": t.get("disc_number") or 1,
        }

    async def album(self, album_id: str) -> dict:
        async def build() -> dict:
            a = await self.get(f"/albums/{album_id}")
            out = self._album(a)
            tracks, page = [], a.get("tracks") or {}
            while True:
                for t in page.get("items") or []:
                    if t:
                        tracks.append(self._track({**t, "album": a}))
                if not page.get("next"):
                    break
                page = await self.get(page["next"])
            out["track_list"] = tracks
            out["genres"] = a.get("genres") or []
            return out

        return await self.cached(f"album:{album_id}", build)

    async def track(self, track_id: str) -> dict:
        async def build() -> dict:
            return self._track(await self.get(f"/tracks/{track_id}"))

        return await self.cached(f"track:{track_id}", build)


def _image(item: dict, big: bool = False) -> Optional[str]:
    # Spotify lists images largest first (640, 300, 64). Cards use the middle one.
    imgs = item.get("images") or []
    if not imgs:
        return None
    return imgs[0]["url"] if big or len(imgs) == 1 else imgs[1]["url"]


client = SpotifyClient()


# ---------- resolving Spotify items on Qobuz ----------

_gate = asyncio.Semaphore(6)


async def _qobuz(query: str) -> dict:
    async with _gate:
        try:
            return await browser.search(query)
        except BrowseError:
            raise
        except Exception:  # one odd item shouldn't sink the whole page
            return {"albums": [], "tracks": [], "artists": []}


async def _resolve_album(sp: dict) -> Optional[dict]:
    names = sp["artists"]
    resp = await _qobuz(f"{names[0] if names else ''} {sp['title']}".strip())
    best, best_score = None, 0.0
    for c in resp["albums"]:
        t = sim(c["title"], sp["title"])
        if t < MATCH_THRESHOLD or not _artist_ok(c["artist"], names):
            continue
        score = t + (0.1 if sp["tracks"] and c.get("tracks_count") == sp["tracks"] else 0)
        if score > best_score:
            best, best_score = c, score
    return best


async def _resolve_track(sp: dict) -> Optional[dict]:
    names = sp["artists"]
    resp = await _qobuz(f"{names[0] if names else ''} {sp['title']}".strip())
    best, best_score = None, 0.0
    for c in resp["tracks"]:
        t = sim(c["title"], sp["title"])
        if t < MATCH_THRESHOLD or not _artist_ok(c["artist"], names):
            continue
        score = t
        if c.get("duration") and sp["duration"] and abs(c["duration"] - sp["duration"]) <= 3:
            score += 0.15
        if c.get("album") and sp["album"] and sim(c["album"], sp["album"]) >= MATCH_THRESHOLD:
            score += 0.1
        if score > best_score:
            best, best_score = c, score
    return best


PREFIXES = ("sp:", "mb:", "dz:")  # which catalogue an id came from


def is_sp(item_id: str) -> bool:
    """True for any catalogue id (Spotify "sp:", MusicBrainz "mb:"), not just Spotify's."""
    return item_id.startswith(PREFIXES)


def raw(item_id: str) -> str:
    return item_id[3:]


def _client_for(item_id: str):
    if item_id.startswith("dz:"):
        return deezer.client
    return musicbrainz.client if item_id.startswith("mb:") else client


def name_of(c) -> str:
    return "Spotify" if c is client else "Deezer" if c is deezer.client else "MusicBrainz"


def _spotify_ok() -> bool:
    s = config_store.load_settings().spotify
    return bool(s.enabled and s.client_id.strip() and s.client_secret.strip())


def active():
    """The catalogue new searches use, or None when browsing via a catalogue is off."""
    cat = config_store.load_settings().catalog
    if not cat.enabled:
        return None
    if cat.provider == "spotify":
        return client if _spotify_ok() else None
    if cat.provider == "auto" and _spotify_ok():
        return client
    return musicbrainz.client


def available() -> bool:
    return active() is not None


_meta_cache: dict[str, tuple[float, dict]] = {}


async def _artist_meta(item_id: str) -> dict:
    hit = _meta_cache.get(item_id)
    if hit and time.monotonic() - hit[0] < 3600:
        return hit[1]
    meta = await _client_for(item_id).artist(raw(item_id))
    _meta_cache[item_id] = (time.monotonic(), meta)
    return meta


def _kind(group: str, tracks: Optional[int]) -> str:
    if group == "ep":
        return "ep"
    if group == "single":
        return "ep" if (tracks or 1) >= 4 else "single"  # Spotify files EPs under "single"
    return "album"


def album_card(a: dict) -> dict:
    """Spotify album in the same shape the UI uses for Qobuz ones; ids carry "sp:"."""
    return {
        "id": a["p"] + a["id"],
        "title": a["title"],
        "artist": a["artists"][0] if a["artists"] else "Unknown artist",
        "artist_id": a["p"] + a["artist_ids"][0] if a["artist_ids"] else None,
        "cover": a["cover"],
        "cover_large": a["cover_large"],
        "genre": None,
        "year": a["year"],
        "date": a["date"],
        "tracks_count": a["tracks"],
        "duration": None,
        "explicit": False,
        "quality": None,
        "kind": _kind(a["group"], a["tracks"]),
        "primary": a.get("primary", True),  # False: only a guest feature ("appears on")
        "via": a.get("via"),
    }


def track_row(t: dict) -> dict:
    return {
        "id": t["p"] + t["id"],
        "title": t["title"],
        "artist": ", ".join(t["artists"]),
        "artist_id": t["p"] + t["artist_ids"][0] if t["artist_ids"] else None,
        "album": t["album"],
        "album_id": t["p"] + t["album_id"] if t.get("album_id") else None,
        "cover": t["cover"],
        "cover_large": t["cover_large"],
        "duration": t["duration"],
        "explicit": t["explicit"],
        "track_number": t["track_number"],
        "disc": t["disc"],
        "quality": None,
        "date": t["date"],
    }


def artist_card(a: dict) -> dict:
    return {"id": a["p"] + a["id"], "name": a["name"], "image": a.get("image"), "albums_count": None, "sub": a.get("sub", "")}


async def search(query: str) -> dict:
    """Search Spotify only. Nothing here touches Qobuz; Qobuz is looked up when
    something is actually downloaded or previewed."""
    query = query.strip()
    c = active()
    if c is None:
        raise SpotifyError("The reference catalogue is off or isn't set up. Check Settings > Catalogue.")
    sp = await c.search(query)
    artists = sp["artists"]
    # An artist query ("skinz"): show that artist's real discography. Otherwise
    # (a song or album title) use the catalogue's own album hits.
    lead = sp["lead"] if "lead" in sp else (artists[0] if artists and sim(artists[0]["name"], query) >= 0.8 else None)
    albums = list(sp["albums"])
    if lead:
        albums = await full_discography(lead["p"] + lead["id"], 60)
    return {
        "query": query,
        "albums": _dedupe([album_card(a) for a in albums]),
        "artists": [artist_card(a) for a in artists[:6]],
        "tracks": [track_row(t) for t in sp["tracks"]],
        "playlists": [],
        "spotify": {"source": True, "provider": name_of(c)},
    }


# ---------- detail pages (Spotify data, Qobuz only for ids) ----------


async def _lastfm_extras(item_id: str, name: str, disco: list[dict]) -> list[dict]:
    """Releases the catalogue lacks but Last.fm shows real listening for (by exact artist id),
    found on Qobuz so they can be downloaded. MusicBrainz isn't exhaustive; this fills the gaps."""
    if not item_id.startswith("mb:") or not lastfm.available():
        return []
    plays = await lastfm.top_albums(raw(item_id))
    known = [d["title"] for d in disco]
    gaps = [
        (title, n)
        for key, (title, n) in plays.items()
        if n >= lastfm.VOUCH_PLAYS and not any(sim(title, k) >= MATCH_THRESHOLD for k in known)
    ]
    gaps = [t for t, _ in sorted(gaps, key=lambda g: -g[1])[:12]]
    found = await asyncio.gather(*[_resolve_album({"title": t, "artists": [name], "tracks": None}) for t in gaps])
    out = []
    for card in found:
        if card:
            out.append({**card, "primary": True, "kind": card.get("kind") or "album"})
    return out


async def artist_page(item_id: str) -> dict:
    c = _client_for(item_id)
    meta = await _artist_meta(item_id)
    disco = await full_discography(item_id)
    cards = [album_card(a) for a in disco] + await _lastfm_extras(item_id, meta["name"], disco)
    return {
        "id": item_id,
        "name": meta["name"],
        "image": meta.get("image"),
        "bio": meta.get("bio") or "",
        "genres": meta.get("genres") or [],
        "followers": meta.get("followers"),
        "sub": meta.get("sub") or "",
        "albums": sorted(_dedupe(cards), key=lambda c: c.get("date") or "", reverse=True),
        "spotify": {"id": item_id, "source": True, "provider": name_of(c)},
    }


async def album_page(item_id: str) -> dict:
    a = await _client_for(item_id).album(raw(item_id))
    card = album_card(a)
    tracks = [track_row(t) for t in a["track_list"]]
    card.update(
        {
            "cover_large": a["cover_large"],
            "genre": ", ".join(a.get("genres") or []) or None,
            "label": a.get("label"),
            "copyright": a.get("copyright"),
            "tracks": tracks,
            "tracks_count": a["tracks"] or len(tracks),
            "disc_count": max([t["disc"] for t in tracks] or [1]),
            "duration": int(sum(t["duration"] or 0 for t in tracks)) or None,
        }
    )
    card["source"] = await source_for("album", item_id)
    return card


async def track_page(item_id: str) -> dict:
    row = track_row(await _client_for(item_id).track(raw(item_id)))
    row["source"] = await source_for("track", item_id)
    return row


# ---------- turning "sp:" ids into Qobuz ids (download / preview time) ----------

_resolved: dict[str, tuple[float, str]] = {}


def _remember(key: str, value: str) -> str:
    _resolved[key] = (time.monotonic(), value)
    return value


def _recall(key: str) -> Optional[str]:
    hit = _resolved.get(key)
    return hit[1] if hit and time.monotonic() - hit[0] < 3600 else None


_cards: dict[str, tuple[float, Optional[dict]]] = {}
_UNSET = object()


def _card_get(key: str):
    hit = _cards.get(key)
    return hit[1] if hit and time.monotonic() - hit[0] < 1800 else _UNSET


async def qobuz_card(kind: str, item_id: str) -> Optional[dict]:
    """The Qobuz album/track card matching this Spotify item, or None. Misses are cached too."""
    key = f"{kind}:{item_id}"
    hit = _card_get(key)
    if hit is not _UNSET:
        return hit
    if kind == "album":
        card = await _resolve_album(await _client_for(item_id).album(raw(item_id)))
    else:
        card = await _resolve_track(await _client_for(item_id).track(raw(item_id)))
    _cards[key] = (time.monotonic(), card)
    return card


async def qobuz_album_id(item_id: str) -> str:
    card = await qobuz_card("album", item_id)
    if not card:
        raise BrowseError("This release isn't available on Qobuz.", 404)
    return card["id"]


async def qobuz_track_id(item_id: str) -> str:
    card = await qobuz_card("track", item_id)
    if not card:
        raise BrowseError("This song isn't available on Qobuz.", 404)
    return card["id"]


def youtube_fallback() -> bool:
    y = config_store.load_settings().youtube
    return bool(y.enabled and y.fallback)


async def source_for(kind: str, item_id: str) -> dict:
    """Where a download of this item would come from, and at what quality."""
    card = await qobuz_card(kind, item_id)
    if card:
        return {"source": "qobuz", "id": card["id"], "quality": card.get("quality")}
    if youtube_fallback():
        return {"source": "youtube", "quality": "lossy"}
    return {"source": None}


async def availability(albums: list[str], tracks: list[str]) -> dict:
    async def one(kind: str, item_id: str):
        try:
            return item_id, await source_for(kind, item_id)
        except (SpotifyError, BrowseError):
            return item_id, {"source": None, "unknown": True}

    a = await asyncio.gather(*[one("album", i) for i in albums])
    t = await asyncio.gather(*[one("track", i) for i in tracks])
    return {"albums": dict(a), "tracks": dict(t)}


# ---------- YouTube fallback ----------

_BAD_WORDS = re.compile(r"\b(live|remix|cover|karaoke|instrumental|sped up|slowed|reverb|nightcore|8d|lyrics?)\b")


def _score_video(v: dict, t: dict) -> float:
    want = norm(t["title"])
    title = norm(v["title"])
    score = 0.0
    if t["duration"] and v.get("duration"):
        diff = abs(v["duration"] - t["duration"])
        if diff > 15:
            return 0.0
        score += 0.5 if diff <= 4 else 0.2
    score += 0.3 if want and want in title else 0.3 * sim(t["title"], v["title"])
    channel = norm(v.get("channel"))
    if (v.get("channel") or "").endswith(" - Topic"):
        score += 0.3  # YouTube's auto-generated, label-supplied audio
    elif any(norm(a) and norm(a) in channel for a in t["artists"]):
        score += 0.15
    if _BAD_WORDS.search(title) and not _BAD_WORDS.search(norm(t["title"])):
        score -= 0.6
    return score


async def youtube_match(t: dict) -> Optional[dict]:
    from . import youtube

    try:
        results = await youtube.search(f"{t['artists'][0] if t['artists'] else ''} {t['title']}".strip(), limit=8)
    except youtube.YoutubeError:
        return None
    scored = sorted(((_score_video(v, t), v) for v in results), key=lambda x: x[0], reverse=True)
    return scored[0][1] if scored and scored[0][0] >= 0.6 else None


async def youtube_plan(kind: str, item_id: str) -> dict:
    """One YouTube video per Spotify track, with the Spotify tags to apply to it."""
    c = _client_for(item_id)
    if kind == "album":
        album = await c.album(raw(item_id))
        tracks = album["track_list"]
    else:
        track = await c.track(raw(item_id))
        album = await c.album(track["album_id"]) if track.get("album_id") else None
        tracks = [track]
    gate = asyncio.Semaphore(3)

    async def find(t: dict):
        async with gate:
            return t, await youtube_match(t)

    items, missing = [], []
    for t, video in await asyncio.gather(*[find(t) for t in tracks]):
        if not video:
            missing.append(t["title"])
            continue
        tags = {
            "title": t["title"],
            "artist": ", ".join(t["artists"]),
            "track_number": t["track_number"],
        }
        if album:
            tags.update(
                {
                    "album": album["title"],
                    "album_artist": ", ".join(album["artists"]),
                    "year": album["year"],
                    "genre": (album.get("genres") or [None])[0],
                }
            )
        items.append({"video": video, "tags": tags, "label": f"{tags['artist']} - {t['title']}"})
    return {"items": items, "missing": missing, "cover_url": (album or {}).get("cover_large")}


async def qobuz_artist_id(item_id: str) -> Optional[str]:
    """The Qobuz artist whose releases line up with this Spotify artist's, for monitoring."""
    if (hit := _recall(item_id)) is not None:
        return hit
    sid = raw(item_id)
    name = (await _artist_meta(item_id))["name"]
    disco = (await _client_for(item_id).discography(sid))[:8]
    hits = [c for c in await asyncio.gather(*[_resolve_album(a) for a in disco]) if c]
    ids = [c["artist_id"] for c in hits if c.get("artist_id") and _artist_ok(c["artist"], [name])]
    return _remember(item_id, max(set(ids), key=ids.count)) if ids else None


async def resolve_ids(media_type: str, item_id: str) -> str:
    """Download requests: swap a Spotify id for the Qobuz one (others pass through)."""
    if not is_sp(item_id):
        return item_id
    if media_type == "album":
        return await qobuz_album_id(item_id)
    if media_type == "track":
        return await qobuz_track_id(item_id)
    raise BrowseError("Only albums and songs can be downloaded from Spotify results.", 400)


def _dedupe(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for c in items:
        if c["id"] not in seen:
            seen.add(c["id"])
            out.append(c)
    return out


async def _artist_card(qobuz_id: str, name: str) -> dict:
    resp = await _qobuz(name)
    for c in resp["artists"]:
        if c["id"] == qobuz_id:
            return dict(c)
    return {"id": qobuz_id, "name": name, "image": None, "albums_count": None}


async def find_artist_id(name: str, qobuz_albums: list[dict]) -> Optional[str]:
    """The catalogue artist (as a prefixed id) that is this Qobuz artist: same name, and the
    discography that overlaps most with the Qobuz releases (same-name artists exist)."""
    c = active()
    if c is None:
        return None
    sp = await c.search(name)
    best, best_score = None, 0
    for cand in [a for a in sp["artists"] if sim(a["name"], name) >= 0.9][:3]:
        titles = [d["title"] for d in await c.discography(cand["id"])]
        score = sum(1 for a in qobuz_albums if any(sim(a["title"], t) >= MATCH_THRESHOLD for t in titles))
        if score > best_score:
            best, best_score = cand["p"] + cand["id"], score
    return best


async def full_discography(item_id: str, limit: int = DISCOGRAPHY_ALL) -> list[dict]:
    """The catalogue's releases for an artist, plus (MusicBrainz only) the ones Deezer adds."""
    base = await _client_for(item_id).discography(raw(item_id), limit)
    if item_id.startswith("mb:") and config_store.load_settings().catalog.include_deezer:
        try:
            name = (await _artist_meta(item_id))["name"]
            base = base + await deezer.client.extras(name, base)
        except CatalogError:
            pass  # Deezer is a bonus; never fail the page over it
    return base


discography_for = full_discography


# ---------- filtering a Qobuz artist page ----------


async def filter_artist(page: dict, artist_id: Optional[str]) -> dict:
    """Hide releases on a Qobuz artist page that the catalogue's discography doesn't list
    (unless Last.fm shows real listening for them), and swap in the catalogue's artist
    details. Finds the catalogue artist itself when the page wasn't reached from a search."""
    if not artist_id:
        artist_id = await find_artist_id(page["name"], [a for a in page["albums"] if a.get("primary")] or page["albums"])
    elif ":" not in artist_id:
        artist_id = "sp:" + artist_id  # ids stored before there was a second catalogue
    if not artist_id:
        return {**page, "bio": "", "spotify": {"id": None, "hidden": 0}}
    c = _client_for(artist_id)
    titles = [d["title"] for d in await full_discography(artist_id, 300)]
    plays = await lastfm.top_albums(raw(artist_id)) if artist_id.startswith("mb:") else {}
    vouched = [t for t, n in plays.values() if n >= lastfm.VOUCH_PLAYS]
    kept, hidden = [], 0
    for a in page["albums"]:
        if not a.get("primary") or any(sim(a["title"], t) >= MATCH_THRESHOLD for t in titles + vouched):
            kept.append(a)
        else:
            hidden += 1
    meta: dict = {}
    try:
        meta = await _artist_meta(artist_id)
    except SpotifyError:
        pass  # the filtered list is still useful without the extras
    return {
        **page,
        "albums": kept,
        "image": meta.get("image") or page.get("image"),
        # Only the catalogue's own bio; the Qobuz one is dropped rather than mixed in.
        "bio": meta.get("bio") or "",
        "genres": meta.get("genres") or [],
        "followers": meta.get("followers"),
        "spotify": {"id": artist_id, "hidden": hidden, "provider": name_of(c)},
    }
