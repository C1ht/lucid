"""MusicBrainz as the reference catalogue: free, no key, curated by volunteers
(same-name artists are kept apart with a note like "Danish rapper").

Etiquette: one request per second and a descriptive User-Agent, so every call
goes through one throttle and results are cached for hours. Cover art comes
from the Cover Art Archive, artist photos and bios from Wikipedia (when
MusicBrainz links to it), with Deezer as a last-resort photo source.
"""

import asyncio
import re
import time
from typing import Any, Optional
from urllib.parse import quote

import httpx

from . import lastfm
from .catalog_base import CatalogError, norm, sim

API = "https://musicbrainz.org/ws/2"
UA = "Lucid/1.0 (+https://github.com/C1ht/lucid)"
CAA = "https://coverartarchive.org/release-group"
PREFIX = "mb:"
CACHE_TTL = 6 * 3600
KINDS = {"Album": "album", "Single": "single", "EP": "ep"}
_LUCENE = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')


def _q(text: str) -> str:
    return re.sub(r"\s+", " ", _LUCENE.sub(" ", text)).strip()


def _names(credit: list[dict]) -> tuple[list[str], list[str]]:
    names, ids = [], []
    for c in credit or []:
        a = c.get("artist") or {}
        name = a.get("name") or c.get("name")
        if name:
            names.append(name)
        if a.get("id"):
            ids.append(a["id"])
    return names, ids


def _cover(rg_id: str, big: bool) -> str:
    return f"{CAA}/{rg_id}/front-{500 if big else 250}"


class MusicBrainz:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._cache: dict[str, tuple[float, Any]] = {}

    async def get(self, path: str, params: Optional[dict] = None) -> dict:
        params = {**(params or {}), "fmt": "json"}
        ck = f"{path}?{sorted(params.items())}"
        hit = self._cache.get(ck)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        async with self._lock:
            hit = self._cache.get(ck)
            if hit and time.monotonic() - hit[0] < CACHE_TTL:
                return hit[1]
            for attempt in range(4):
                wait = self._last + 1.1 - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    async with httpx.AsyncClient(timeout=25, headers={"User-Agent": UA}) as http:
                        r = await http.get(f"{API}{path}", params=params)
                except httpx.HTTPError as exc:
                    raise CatalogError(f"Couldn't reach MusicBrainz ({type(exc).__name__}).") from exc
                finally:
                    self._last = time.monotonic()
                if r.status_code in (429, 503):
                    await asyncio.sleep(2 * (attempt + 1))  # asked to slow down
                    continue
                break
            else:
                raise CatalogError("MusicBrainz is busy right now. Try again in a moment.")
        if r.status_code == 404:
            raise CatalogError("Not found on MusicBrainz.")
        if r.status_code != 200:
            raise CatalogError(f"MusicBrainz error {r.status_code}.")
        data = r.json()
        self._cache[ck] = (time.monotonic(), data)
        if len(self._cache) > 400:
            for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[:100]:
                self._cache.pop(k, None)
        return data

    # ---- normalising ----

    @staticmethod
    def _rg(rg: dict) -> dict:
        names, ids = _names(rg.get("artist-credit"))
        date = rg.get("first-release-date") or ""
        return {
            "p": PREFIX,
            "id": rg["id"],
            "title": rg.get("title") or "",
            "artists": names,
            "artist_ids": ids,
            "tracks": None,
            "year": date[:4],
            "date": date,
            "cover": _cover(rg["id"], False),
            "cover_large": _cover(rg["id"], True),
            "group": KINDS.get(rg.get("primary-type") or "", "album"),
            "label": None,
            "copyright": "",
        }

    @staticmethod
    def _track_from(rec: dict, album: Optional[dict] = None, number: Optional[int] = None, disc: int = 1) -> dict:
        names, ids = _names(rec.get("artist-credit"))
        if not names and album:
            names, ids = album["artists"], album["artist_ids"]
        return {
            "p": PREFIX,
            "id": rec["id"],
            "title": rec.get("title") or "",
            "artists": names,
            "artist_ids": ids,
            "album": (album or {}).get("title", ""),
            "album_id": (album or {}).get("id"),
            "cover": (album or {}).get("cover"),
            "cover_large": (album or {}).get("cover_large"),
            "date": (album or {}).get("date", ""),
            "duration": (rec.get("length") or 0) / 1000,
            "explicit": False,
            "track_number": number,
            "disc": disc,
        }

    # ---- catalogue interface ----

    async def search(self, query: str) -> dict:
        q = _q(query)
        if not q:
            return {"artists": [], "albums": [], "tracks": [], "lead": None}
        arts = (await self.get("/artist", {"query": q, "limit": 6})).get("artists") or []
        artists = []
        for a in arts:
            sub = ", ".join(x for x in [a.get("disambiguation"), a.get("country")] if x)
            artists.append({"p": PREFIX, "id": a["id"], "name": a["name"], "popularity": a.get("score", 0), "image": None, "sub": sub})
        confident = [a for a in artists if sim(a["name"], query) >= 0.9 and a["popularity"] >= 90]
        # Several artists share the name: don't guess which one is meant, let the user pick.
        lead = confident[0] if len(confident) == 1 else None

        albums: list[dict] = []
        if not lead:
            rgs = (await self.get("/release-group", {"query": q, "limit": 10})).get("release-groups") or []
            albums = [self._rg(r) for r in rgs if r.get("primary-type") in KINDS]
        tracks = []
        # An artist-name query wants that artist's songs, not other artists' songs that share the word.
        rec_query = f'artist:"{q}"' if confident else q
        for r in (await self.get("/recording", {"query": rec_query, "limit": 10})).get("recordings") or []:
            rel = next((x for x in r.get("releases") or [] if (x.get("release-group") or {}).get("primary-type") in KINDS), None)
            album = None
            if rel:
                rg = rel["release-group"]
                album = {
                    "id": rg["id"], "title": rg.get("title") or rel.get("title") or "",
                    "artists": [], "artist_ids": [], "date": rel.get("date") or "",
                    "cover": _cover(rg["id"], False), "cover_large": _cover(rg["id"], True),
                }
            tracks.append(self._track_from(r, album))
        return {"artists": artists, "albums": albums, "tracks": tracks, "lead": lead}

    async def discography(self, artist_id: str, limit: int = 40) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while len(out) < limit:
            d = await self.get("/release-group", {"artist": artist_id, "inc": "artist-credits", "limit": 100, "offset": offset})
            out += [self._rg(r) for r in d.get("release-groups") or [] if r.get("primary-type") in KINDS]
            offset += 100
            if offset >= d.get("release-group-count", 0):
                break
        return out[:limit]

    async def artist(self, artist_id: str) -> dict:
        a = await self.get(f"/artist/{artist_id}", {"inc": "url-rels+genres"})
        meta: dict = {
            "name": a.get("name") or "",
            "image": None,
            "genres": [g["name"] for g in sorted(a.get("genres") or [], key=lambda g: -g.get("count", 0))][:5],
            "followers": None,
            "bio": "",
            "sub": a.get("disambiguation") or "",
            "mbid": artist_id,
        }
        wiki = await _wikipedia(a.get("relations") or [])
        if wiki:
            meta["bio"], meta["image"] = wiki.get("extract", ""), wiki.get("image")
        if not meta["bio"]:
            bio, _ = await lastfm.artist_bio(artist_id)
            meta["bio"] = bio
        if not meta["image"]:
            titles = [d["title"] for d in await self.discography(artist_id)]
            meta["image"] = await _deezer_photo(meta["name"], titles)
        return meta

    async def album(self, album_id: str) -> dict:
        rg = await self.get(f"/release-group/{album_id}", {"inc": "artist-credits+releases"})
        out = self._rg(rg)
        releases = rg.get("releases") or []
        official = [r for r in releases if r.get("status") == "Official"] or releases
        # Earliest official release: the original track list, not a later deluxe edition.
        pick = sorted(official, key=lambda r: (r.get("date") or "9999", r.get("id")))[0] if official else None
        tracks: list[dict] = []
        if pick:
            rel = await self.get(f"/release/{pick['id']}", {"inc": "recordings+artist-credits+labels"})
            labels = rel.get("label-info") or []
            out["label"] = ((labels[0].get("label") or {}).get("name")) if labels else None
            for disc in rel.get("media") or []:
                for t in disc.get("tracks") or []:
                    rec = t.get("recording") or {}
                    if not rec.get("id"):
                        continue
                    row = self._track_from(
                        {**rec, "title": t.get("title") or rec.get("title"), "length": t.get("length") or rec.get("length"),
                         "artist-credit": t.get("artist-credit") or rec.get("artist-credit")},
                        out, t.get("position"), disc.get("position") or 1,
                    )
                    tracks.append(row)
        out["tracks"] = len(tracks) or None
        out["track_list"] = tracks
        out["genres"] = []
        return out

    async def track(self, track_id: str) -> dict:
        r = await self.get(f"/recording/{track_id}", {"inc": "artist-credits+releases+release-groups"})
        rel = next((x for x in r.get("releases") or [] if (x.get("release-group") or {}).get("primary-type") in KINDS), None)
        album = None
        if rel:
            rg = rel["release-group"]
            album = {
                "id": rg["id"], "title": rg.get("title") or rel.get("title") or "", "artists": [], "artist_ids": [],
                "date": rel.get("date") or "", "cover": _cover(rg["id"], False), "cover_large": _cover(rg["id"], True),
            }
        return self._track_from(r, album)


# ---------- artist photo / bio helpers (no MusicBrainz quota used) ----------


async def _wikipedia(relations: list[dict]) -> Optional[dict]:
    title, lang = None, "en"
    for rel in relations:
        url = (rel.get("url") or {}).get("resource", "")
        if rel.get("type") == "wikipedia":
            m = re.match(r"https?://([a-z\-]+)\.wikipedia\.org/wiki/(.+)", url)
            if m:
                lang, title = m.group(1), m.group(2)
                break
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA}) as http:
            if not title:
                wd = next(((r.get("url") or {}).get("resource", "") for r in relations if r.get("type") == "wikidata"), "")
                qid = wd.rstrip("/").rsplit("/", 1)[-1]
                if not re.fullmatch(r"Q\d+", qid):
                    return None
                ent = (await http.get(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json")).json()
                links = ((ent.get("entities") or {}).get(qid) or {}).get("sitelinks") or {}
                site = links.get("enwiki") or next((v for k, v in links.items() if k.endswith("wiki") and k not in ("commonswiki", "wikidatawiki")), None)
                if not site:
                    return None
                lang = site["site"][:-4].replace("_", "-")
                title = quote(site["title"].replace(" ", "_"))
            r = await http.get(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}")
        if r.status_code != 200:
            return None
        d = r.json()
        if d.get("type") == "disambiguation":
            return None
        return {"extract": d.get("extract") or "", "image": (d.get("thumbnail") or {}).get("source")}
    except (httpx.HTTPError, ValueError, KeyError):
        return None


async def _deezer_photo(name: str, titles: list[str]) -> Optional[str]:
    """Deezer's photo, but only for the Deezer artist whose albums overlap this discography."""
    if not titles:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            found = (await http.get("https://api.deezer.com/search/artist", params={"q": name, "limit": 5})).json().get("data") or []
            for cand in [c for c in found if sim(c.get("name", ""), name) >= 0.9][:3]:
                albums = (await http.get(f"https://api.deezer.com/artist/{cand['id']}/albums", params={"limit": 100})).json().get("data") or []
                if any(sim(a.get("title", ""), t) >= 0.86 for a in albums for t in titles):
                    pic = cand.get("picture_big") or cand.get("picture_medium")
                    return pic if pic and "/images/artist//" not in pic else None
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    return None


client = MusicBrainz()
