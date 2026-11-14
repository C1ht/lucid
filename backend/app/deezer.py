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


def overlap(dz_titles: list[str], mb_titles: list[str]) -> bool:
    """Do these two discographies belong to the same artist? One shared title ("911", "Intro")
    proves nothing for a common name, so require a real overlap."""
    if not dz_titles or not mb_titles:
        return False
    matches = sum(1 for m in mb_titles if any(sim(t, m) >= MATCH_THRESHOLD for t in dz_titles))
    return matches >= min(2, len(mb_titles)) and matches >= 0.2 * min(len(mb_titles), len(dz_titles))


class Deezer:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, Any]] = {}
        self._gate = asyncio.Semaphore(4)  # Deezer allows ~50 requests per 5 s
        self._pace = asyncio.Lock()
        self._next = 0.0

    async def get(self, path: str, params: Optional[dict] = None) -> dict:
        ck = f"{path}?{sorted((params or {}).items())}"
        hit = self._cache.get(ck)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        async with self._gate:
            async with self._pace:  # start requests ~9/s apart so bulk lookups stay under the quota
                wait = self._next - time.monotonic()
                self._next = max(self._next, time.monotonic()) + 0.11
            if wait > 0:
                await asyncio.sleep(wait)
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

    async def isrc(self, album_id: str) -> Optional[str]:
        """The ISRC of an album's first track. The code starts with the country and the rights
        holder, so it tells a label's own releases from a same-name artist's."""
        for attempt in (0, 1):
            try:
                rows = (await self.get(f"/album/{album_id}/tracks", {"limit": 1})).get("data") or []
                return (rows[0].get("isrc") or None) if rows else None
            except CatalogError:
                if attempt:
                    return None
                await asyncio.sleep(1.5)  # most likely the quota; try once more
        return None

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

    async def artist(self, artist_id: str) -> dict:
        a = await self.get(f"/artist/{artist_id}")
        pic = a.get("picture_big") or a.get("picture_medium")
        return {
            "name": a.get("name") or "",
            "image": pic if pic and "/images/artist//" not in pic else None,
            "genres": [],
            "followers": a.get("nb_fan"),
            "bio": "",
            "sub": f"{a.get('nb_album')} releases" if a.get("nb_album") else "",
        }

    async def candidates(self, name: str, mb_titles: list[list[str]]) -> tuple[list[dict], dict[int, str]]:
        """Notable Deezer artists with this name that MusicBrainz doesn't account for.

        Returns (artist cards for Deezer-only artists, {index of MB artist: Deezer photo}).
        A Deezer artist whose albums overlap one of the MusicBrainz artists' (`mb_titles`) is
        that same artist, so it only lends its photo. Tiny same-name accounts are ignored."""
        try:
            found = (await self.get("/search/artist", {"q": name, "limit": 10})).get("data") or []
            notable = [
                c for c in found
                if sim(c.get("name", ""), name) >= 0.9 and (c.get("nb_fan") or 0) >= 50
            ][:5]
            extra, photos = [], {}
            for c in notable:
                albums = await self._paged(f"/artist/{c['id']}/albums", 100)
                titles = [a.get("title", "") for a in albums]
                owner = next((i for i, mb in enumerate(mb_titles) if overlap(titles, mb)), None)
                pic = c.get("picture_medium")
                pic = pic if pic and "/images/artist//" not in pic else None
                if owner is not None:
                    photos.setdefault(owner, pic)
                    continue
                latest = max(albums, key=lambda a: a.get("release_date") or "", default=None)
                sub = f"{c.get('nb_album') or len(albums)} releases"
                if latest:
                    sub += f", latest “{latest.get('title')}” ({(latest.get('release_date') or '')[:4]})"
                extra.append({"p": PREFIX, "id": str(c["id"]), "name": c["name"], "sub": sub, "image": pic, "popularity": c.get("nb_fan") or 0})
            return extra, photos
        except CatalogError:
            return [], {}

    # ---- matching a MusicBrainz artist to its Deezer artist ----

    async def find_artist(self, name: str, titles: list[str], strict: bool = False) -> Optional[dict]:
        """The Deezer artist with this name whose albums overlap `titles`, or None. With
        `strict`, an unreachable Deezer raises instead of looking like "no such artist"."""
        if not titles:
            return None
        try:
            found = (await self.get("/search/artist", {"q": name, "limit": 6})).get("data") or []
            best, best_score = None, 0
            for cand in [c for c in found if sim(c.get("name", ""), name) >= 0.9][:4]:
                albums = await self._paged(f"/artist/{cand['id']}/albums", 100)
                dz_titles = [a.get("title", "") for a in albums]
                if not overlap(dz_titles, titles):
                    continue
                score = sum(1 for t in titles if any(sim(x, t) >= MATCH_THRESHOLD for x in dz_titles))
                if score > best_score:
                    best, best_score = cand, score
            return best
        except CatalogError:
            if strict:
                raise
            return None

    async def releases_for(self, name: str, base: list[dict]) -> Optional[list[dict]]:
        """All Deezer releases of the Deezer artist matching `base` (MusicBrainz), or None
        when no Deezer artist overlaps it (then there is nothing to cross-check against)."""
        artist = await self.find_artist(name, [a["title"] for a in base])
        if not artist:
            return None
        return await self.discography(str(artist["id"]), 300, artist)


client = Deezer()
