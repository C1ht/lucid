"""Artist monitoring: when an artist is added, and then once a day at a set
time, compare their Qobuz releases against streamrip's downloads database and
queue every release that has tracks we don't have yet.

A release is handled once: it is queued (or found complete) and remembered, so
later checks only look at releases that are new to the artist's page. A queued
release whose download failed is retried at the next check. Timing uses the
process's local time; set the TZ environment variable on the container to
schedule in your own timezone.
"""

import asyncio
import json
import re
from datetime import date, datetime
from typing import Any, Optional

from . import paths
from .library import downloaded_ids
from . import spotify
from .browse import BrowseError, browser
from .jobs import INTERRUPTED, manager

TICK_SECONDS = 30
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _now() -> datetime:
    return datetime.now().astimezone()


def _releases(artist: dict) -> list[dict]:
    """Releases credited to the artist themselves (not 'appears on'), falling
    back to everything for artists like labels that have none as primary."""
    albums = artist.get("albums") or []
    primary = [a for a in albums if a.get("primary")]
    return primary or albums


def _is_released(album: dict) -> bool:
    # Qobuz lists upcoming albums early; downloading one would just fail, so
    # wait until its release date arrives and leave it unrecorded until then.
    return not album.get("date") or album["date"][:10] <= date.today().isoformat()


class Monitor:
    def __init__(self) -> None:
        self.enabled = False
        self.time = "03:00"
        self.last_run_date: Optional[str] = None
        self.last_run_at: Optional[str] = None
        self.artists: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._syncs: set[asyncio.Task] = set()
        self._load()

    # ---------- persistence ----------

    def _load(self) -> None:
        try:
            raw = json.loads(paths.MONITORS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.enabled = bool(raw.get("enabled", False))
        self.time = raw.get("time", self.time) if _TIME_RE.match(raw.get("time", "")) else self.time
        self.last_run_date = raw.get("last_run_date")
        self.last_run_at = raw.get("last_run_at")
        self.artists = {a["id"]: a for a in raw.get("artists", []) if a.get("id")}

    def _save(self) -> None:
        paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        paths.MONITORS_PATH.write_text(
            json.dumps(
                {
                    "enabled": self.enabled,
                    "time": self.time,
                    "last_run_date": self.last_run_date,
                    "last_run_at": self.last_run_at,
                    "artists": list(self.artists.values()),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def state(self) -> dict:
        artists = sorted(self.artists.values(), key=lambda a: a["name"].lower())
        return {
            "enabled": self.enabled,
            "time": self.time,
            "last_run_at": self.last_run_at,
            "timezone": _now().tzname(),
            "spotify_available": spotify.available(),
            "artists": [{k: v for k, v in a.items() if k not in ("known", "jobs")} for a in artists],
        }

    # ---------- management ----------

    def set_schedule(self, enabled: bool, time: str) -> dict:
        if not _TIME_RE.match(time):
            raise ValueError("Time must be HH:MM (24-hour)")
        self.enabled, self.time = enabled, time
        self._save()
        return self.state()

    async def add_artist(self, artist_id: str, use_spotify: Optional[bool] = None, spotify_id: Optional[str] = None) -> dict:
        artist = await browser.artist(artist_id, fresh=True)
        if use_spotify is None:
            use_spotify = spotify.available()  # on by default whenever the reference catalogue is on
        self.artists[artist_id] = {
            "spotify": use_spotify,
            "spotify_id": spotify_id,
            "id": artist_id,
            "name": artist["name"],
            "image": artist.get("image"),
            "added_at": _now().isoformat(),
            "known": [],
            "jobs": {},
            "last_checked": None,
            "last_found": None,
            "last_error": None,
        }
        self._save()
        # Walking a whole back catalogue takes a while; don't hold up the request.
        task = asyncio.create_task(self._sync_one(artist_id))
        self._syncs.add(task)
        task.add_done_callback(self._syncs.discard)
        return self.state()

    def set_spotify(self, artist_id: str, use_spotify: bool) -> dict:
        entry = self.artists.get(artist_id)
        if entry:
            entry["spotify"] = use_spotify
            self._save()
            # Releases the filter was hiding (or now hides) are picked up straight away.
            task = asyncio.create_task(self._sync_one(artist_id))
            self._syncs.add(task)
            task.add_done_callback(self._syncs.discard)
        return self.state()

    def remove_artist(self, artist_id: str) -> dict:
        self.artists.pop(artist_id, None)
        self._save()
        return self.state()

    # ---------- checking ----------

    async def run_check(self) -> None:
        async with self._lock:
            for entry in list(self.artists.values()):
                if entry["id"] not in self.artists:
                    continue
                await self._check_artist(entry)
                await asyncio.sleep(1)  # be gentle with the API
            self.last_run_at = _now().isoformat()
            self.last_run_date = _now().date().isoformat()
            self._save()

    async def check_one(self, artist_id: str) -> dict:
        """Check a single monitored artist right now (the per-artist "Check now")."""
        if artist_id not in self.artists:
            raise KeyError(artist_id)
        await self._sync_one(artist_id)
        return self.state()

    async def _sync_one(self, artist_id: str) -> None:
        async with self._lock:
            entry = self.artists.get(artist_id)
            if entry:
                await self._check_artist(entry)
                self._save()

    def _forget_failed(self, entry: dict) -> None:
        """Let releases whose download failed (or was interrupted) be tried again."""
        for album_id, job_id in list(entry["jobs"].items()):
            job = manager.jobs.get(job_id)
            # A download the user cancelled themselves stays cancelled.
            if job and (job.status == "failed" or (job.status == "canceled" and job.error == INTERRUPTED)):
                del entry["jobs"][album_id]
                if album_id in entry["known"]:
                    entry["known"].remove(album_id)

    async def _check_artist(self, entry: dict) -> None:
        entry["last_checked"] = _now().isoformat()
        entry.setdefault("jobs", {})
        self._forget_failed(entry)
        try:
            artist = await browser.artist(entry["id"], fresh=True)
        except BrowseError as exc:
            entry["last_error"] = str(exc)
            return
        entry["last_error"] = None
        entry["name"] = artist["name"]

        if entry.get("spotify") and spotify.available():
            try:
                if not entry.get("spotify_id"):
                    entry["spotify_id"] = await spotify.find_artist_id(artist["name"], _releases(artist))
                if entry.get("spotify_id"):
                    artist = await spotify.filter_artist(artist, entry["spotify_id"])
                    entry["image"] = artist.get("image") or entry.get("image")
                else:
                    entry["last_error"] = "Not found on Spotify, so all Qobuz releases are monitored."
            except (spotify.SpotifyError, BrowseError) as exc:
                # Never queue unverified releases when the filter is on; try again next check.
                entry["last_error"] = f"Spotify: {exc} Will retry at the next check."
                return

        have = downloaded_ids()
        known = set(entry["known"])
        queued: list[str] = []
        for album in _releases(artist):
            if album["id"] in known or not _is_released(album):
                continue
            try:
                detail = await browser.album(album["id"])
            except BrowseError as exc:
                entry["last_error"] = str(exc)  # leave it unrecorded; retried next check
                continue
            await asyncio.sleep(0.3)  # be gentle with the API
            entry["known"].append(album["id"])
            if all(t["id"] in have for t in detail["tracks"]):
                continue
            label = f"{album['title']} by {album['artist']}"
            job = manager.enqueue(
                "id",
                f"[Monitor] {label}",
                {"source": "qobuz", "media_type": "album", "id": album["id"], "desc": label},
            )
            entry["jobs"][album["id"]] = job.id
            queued.append(album["title"])
        if queued:
            title = queued[0] if len(queued) == 1 else f"{len(queued)} releases"
            entry["last_found"] = {"title": title, "at": _now().isoformat()}

    # ---------- scheduler ----------

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    def _due(self) -> bool:
        if not self.enabled or not self.artists:
            return False
        now = _now()
        hh, mm = map(int, self.time.split(":"))
        today = now.date().isoformat()
        return self.last_run_date != today and (now.hour, now.minute) >= (hh, mm)

    async def _loop(self) -> None:
        while True:
            try:
                if self._due() and not self._lock.locked():
                    await self.run_check()
            except Exception:  # noqa: BLE001 - a bad check must not kill the scheduler
                pass
            await asyncio.sleep(TICK_SECONDS)


monitor = Monitor()
