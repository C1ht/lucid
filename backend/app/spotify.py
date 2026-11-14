"""Spotify as a *reference catalogue* for search -- nothing is ever downloaded from it.

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

from . import config_store
from .browse import BrowseError, browser

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
CACHE_TTL = 600
MATCH_THRESHOLD = 0.86
# Search results are small in recent Spotify API versions; page through
# anything longer with `next` instead of relying on a large `limit`.
PAGE = 10
MAX_DISCOGRAPHY = 40


class SpotifyError(Exception):
    pass


# ---------- name matching ----------

_PAREN = re.compile(r"[\(\[][^)\]]*[\)\]]")
_SUFFIX = re.compile(r"\s+-\s+.*$")
_FEAT = re.compile(r"\b(feat|ft|featuring|with)\b.*$")


def norm(text: Optional[str]) -> str:
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = _PAREN.sub(" ", s)
    s = _SUFFIX.sub("", s)  # "Song - Remastered 2011", "Song - Radio Edit"
    s = s.replace("&", " and ")
    s = _FEAT.sub("", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def sim(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


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
                "image": _image(a),
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
        return {
            "id": a.get("id"),
            "title": a.get("name") or "",
            "artists": [x.get("name") for x in a.get("artists") or [] if x.get("name")],
            "tracks": a.get("total_tracks"),
            "year": (a.get("release_date") or "")[:4],
        }

    @staticmethod
    def _track(t: dict) -> dict:
        album = t.get("album") or {}
        return {
            "id": t.get("id"),
            "title": t.get("name") or "",
            "artists": [x.get("name") for x in t.get("artists") or [] if x.get("name")],
            "album": album.get("name") or "",
            "duration": (t.get("duration_ms") or 0) / 1000,
        }


def _image(artist: dict) -> Optional[str]:
    # Spotify lists images largest first; the middle one (~320px) suits cards and the page header.
    imgs = artist.get("images") or []
    return (imgs[1] if len(imgs) > 1 else imgs[0])["url"] if imgs else None


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


async def search(query: str) -> dict:
    """Search Spotify, then return only what also exists on Qobuz (Qobuz ids)."""
    query = query.strip()
    sp = await client.search(query)

    artists = sp["artists"]
    # An artist query ("skinz"): use that artist's real discography. Otherwise
    # (a song or album title) use Spotify's own album/track hits.
    lead = artists[0] if artists and sim(artists[0]["name"], query) >= 0.8 else None
    others = [a for a in artists if a is not lead][:2]

    albums_sp = list(sp["albums"])
    if lead:
        albums_sp = await client.discography(lead["id"])
    # Albums by the other artists, just enough to find out who they are on Qobuz.
    extra: dict[str, list[dict]] = {}
    for a in others:
        extra[a["id"]] = [x for x in sp["albums"] if _artist_ok(a["name"], x["artists"])][:4]

    album_hits, track_hits = await asyncio.gather(
        asyncio.gather(*[_resolve_album(a) for a in albums_sp]),
        asyncio.gather(*[_resolve_track(t) for t in sp["tracks"]]),
    )
    albums = _dedupe([c for c in album_hits if c])
    tracks = _dedupe([c for c in track_hits if c])

    # Qobuz artist cards: the Qobuz artist id that owns the releases Spotify vouches for.
    cards = []
    for a in ([lead] if lead else []) + others:
        mine = [c for c, s in zip(album_hits, albums_sp) if c and _artist_ok(a["name"], s["artists"])]
        mine += [c for c in albums if _artist_ok(a["name"], [c["artist"]])]
        ids = [c["artist_id"] for c in mine if c.get("artist_id")]
        if not ids:
            continue
        qid = max(set(ids), key=ids.count)
        card = await _artist_card(qid, a["name"])
        card["spotify_id"] = a["id"]
        card["image"] = a.get("image") or card.get("image")
        if card["id"] not in {x["id"] for x in cards}:
            cards.append(card)

    return {
        "query": query,
        "albums": albums,
        "artists": cards,
        "tracks": tracks,
        "playlists": [],
        "spotify": {
            "albums_found": len(albums),
            "albums_total": len(albums_sp),
            "tracks_found": len(tracks),
            "tracks_total": len(sp["tracks"]),
        },
    }


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


def available() -> bool:
    s = config_store.load_settings().spotify
    return bool(s.enabled and s.client_id.strip() and s.client_secret.strip())


async def find_artist_id(name: str, qobuz_albums: list[dict]) -> Optional[str]:
    """The Spotify artist that is this Qobuz artist: same name, and the
    discography that overlaps most with the Qobuz releases (same-name artists exist)."""
    sp = await client.search(name)
    best, best_score = None, 0
    for cand in [a for a in sp["artists"] if sim(a["name"], name) >= 0.9][:3]:
        titles = [d["title"] for d in await client.discography(cand["id"])]
        score = sum(1 for a in qobuz_albums if any(sim(a["title"], t) >= MATCH_THRESHOLD for t in titles))
        if score > best_score:
            best, best_score = cand["id"], score
    return best


# ---------- filtering a Qobuz artist page ----------


async def filter_artist(page: dict, spotify_artist_id: Optional[str]) -> dict:
    """Hide releases on a Qobuz artist page that Spotify's discography doesn't list,
    and swap in Spotify's artist details. Finds the Spotify artist itself when
    the page wasn't reached from a Spotify search."""
    if not spotify_artist_id:
        spotify_artist_id = await find_artist_id(page["name"], [a for a in page["albums"] if a.get("primary")] or page["albums"])
    if not spotify_artist_id:
        return {**page, "bio": "", "spotify": {"id": None, "hidden": 0}}
    disco = await client.discography(spotify_artist_id)
    titles = [d["title"] for d in disco]
    kept, hidden = [], 0
    for a in page["albums"]:
        if not a.get("primary") or any(sim(a["title"], t) >= MATCH_THRESHOLD for t in titles):
            kept.append(a)
        else:
            hidden += 1
    meta: dict = {}
    try:
        meta = await client.artist(spotify_artist_id)
    except SpotifyError:
        pass  # the filtered list is still useful without the extras
    return {
        **page,
        "albums": kept,
        "image": meta.get("image") or page.get("image"),
        # Spotify's API has no biography; the Qobuz one is dropped rather than mixed in.
        "bio": "",
        "genres": meta.get("genres") or [],
        "followers": meta.get("followers"),
        "spotify": {"id": spotify_artist_id, "hidden": hidden},
    }
