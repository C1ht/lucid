"""Deezer as a *second* source of releases for an artist already identified in
MusicBrainz. Its public API needs no key. MusicBrainz is not exhaustive (small
artists' singles are often missing) and Deezer's artist pages usually are, so
releases Deezer lists for the verified artist are added to the discography.

The Deezer artist is only accepted when its albums overlap the MusicBrainz
discography, so a same-name artist is never used.
"""

import asyncio
import time
from typing import Any, Optional

import httpx

from .catalog_base import MATCH_THRESHOLD, CatalogError, norm, sim

API = "https://api.deezer.com"
PREFIX = "dz:"
CACHE_TTL = 6 * 3600


class Deezer:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, Any]] = {}
        self._gate = asyncio.Semaphore(4)  # Deezer allows ~50 requests per 5 s

    async def get(self, path: str, params: Optional[dict] = None) -> dict:
        ck = f"{path}?{sorted((params or {}).items())}"
        hit = self._cache.get(ck)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        async with self._gate:
            try:
                async with httpx.AsyncClient(timeout=20) as http:
                    r = await http.get(f"{API}{path}", params=params)
                data = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise CatalogError(f"Couldn't reach Deezer ({type(exc).__name__}).") from exc
        if isinstance(data, dict) and "error" in data:
            raise CatalogError("Deezer didn't have that.")
        self._cache[ck] = (time.monotonic(), data)
        if len(self._cache) > 400:
            for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[:100]:
                self._cache.pop(k, None)
        return data

    async def _paged(self, path: str, limit: int) -> list[dict]:
        out: list[dict] = []
        index = 0
        while len(out) < limit:
            d = await self.get(path, {"limit": 100, "index": index})
            out += d.get("data") or []
            index += 100
            if not d.get("next"):
                break
        return out[:limit]

    # ---- normalising ----

    @staticmethod
    def _album(a: dict, artist: Optional[dict] = None) -> dict:
        main = a.get("artist") or artist or {}
        names = [main.get("name")] if main.get("name") else []
        for c in a.get("contributors") or []:
            if c.get("name") and c["name"] not in names:
                names.append(c["name"])
        date = a.get("release_date") or ""
        genres = ((a.get("genres") or {}).get("data")) or []
        return {
            "p": PREFIX,
            "id": str(a["id"]),
            "title": a.get("title") or "",
            "artists": names,
            "artist_ids": [str(main["id"])] if main.get("id") else [],
            "tracks": a.get("nb_tracks"),
            "year": date[:4],
            "date": date,
            "cover": a.get("cover_medium"),
            "cover_large": a.get("cover_xl") or a.get("cover_big"),
            "group": a.get("record_type") or "album",
            "label": a.get("label"),
            "copyright": "",
            "genres": [g["name"] for g in genres if g.get("name")][:1],
            "via": "Deezer",
        }

    @staticmethod
    def _track(t: dict, album: Optional[dict] = None) -> dict:
        names = [t["artist"]["name"]] if (t.get("artist") or {}).get("name") else []
        for c in t.get("contributors") or []:
            if c.get("name") and c["name"] not in names:
                names.append(c["name"])
        alb = album or {}
        ta = t.get("album") or {}
        return {
            "p": PREFIX,
            "id": str(t["id"]),
            "title": t.get("title") or "",
            "artists": names or alb.get("artists", []),
            "artist_ids": [str(t["artist"]["id"])] if (t.get("artist") or {}).get("id") else [],
            "album": alb.get("title") or ta.get("title") or "",
            "album_id": alb.get("id") or (str(ta["id"]) if ta.get("id") else None),
            "cover": alb.get("cover") or ta.get("cover_medium"),
            "cover_large": alb.get("cover_large") or ta.get("cover_xl"),
            "date": alb.get("date") or ta.get("release_date") or "",
            "duration": t.get("duration") or 0,
            "explicit": bool(t.get("explicit_lyrics")),
            "track_number": t.get("track_position"),
            "disc": t.get("disk_number") or 1,
        }

    # ---- catalogue interface (for dz: ids) ----

    async def album(self, album_id: str) -> dict:
        a = await self.get(f"/album/{album_id}")
        out = self._album(a)
        rows = await self._paged(f"/album/{album_id}/tracks", 300)
        out["track_list"] = [self._track(t, out) for t in rows]
        out["tracks"] = a.get("nb_tracks") or len(out["track_list"])
        return out

    async def track(self, track_id: str) -> dict:
        return self._track(await self.get(f"/track/{track_id}"))

    async def discography(self, artist_id: str, limit: int = 300, artist: Optional[dict] = None) -> list[dict]:
        rows = await self._paged(f"/artist/{artist_id}/albums", limit)
        out, seen = [], set()
        for a in rows:
            key = norm(a.get("title"))
            if key in seen:
                continue  # the same release listed in several editions
            seen.add(key)
            out.append(self._album(a, artist))
        return out

    # ---- matching a MusicBrainz artist to its Deezer artist ----

    async def find_artist(self, name: str, titles: list[str]) -> Optional[dict]:
        """The Deezer artist with this name whose albums overlap `titles`, or None."""
        if not titles:
            return None
        try:
            found = (await self.get("/search/artist", {"q": name, "limit": 6})).get("data") or []
            best, best_score = None, 0
            for cand in [c for c in found if sim(c.get("name", ""), name) >= 0.9][:3]:
                albums = await self._paged(f"/artist/{cand['id']}/albums", 100)
                score = sum(1 for t in titles if any(sim(a.get("title", ""), t) >= MATCH_THRESHOLD for a in albums))
                if score > best_score:
                    best, best_score = cand, score
            return best
        except CatalogError:
            return None

    async def extras(self, name: str, base: list[dict]) -> list[dict]:
        """Deezer releases for this artist that `base` (MusicBrainz) doesn't have."""
        artist = await self.find_artist(name, [a["title"] for a in base])
        if not artist:
            return []
        albums = await self.discography(str(artist["id"]), 300, artist)
        return [a for a in albums if not any(sim(a["title"], b["title"]) >= MATCH_THRESHOLD for b in base)]


client = Deezer()
