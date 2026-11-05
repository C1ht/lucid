"""Artist monitoring: once a day at a set time, re-check each monitored
artist's Qobuz release list and queue a download for anything new.

When an artist is first added, their current releases are recorded as already
known, so only releases that appear afterwards are downloaded (never the back
catalogue). Timing uses the process's local time; set the TZ environment
variable on the container to schedule in your own timezone.
"""

import asyncio
import json
import re
from datetime import date, datetime
from typing import Any, Optional

from . import paths
from .browse import BrowseError, browser
from .jobs import manager

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
            "artists": [{k: v for k, v in a.items() if k != "known"} for a in artists],
        }

    # ---------- management ----------

    def set_schedule(self, enabled: bool, time: str) -> dict:
        if not _TIME_RE.match(time):
            raise ValueError("Time must be HH:MM (24-hour)")
        self.enabled, self.time = enabled, time
        self._save()
        return self.state()

    async def add_artist(self, artist_id: str) -> dict:
        artist = await browser.artist(artist_id, fresh=True)
        self.artists[artist_id] = {
            "id": artist_id,
            "name": artist["name"],
            "image": artist.get("image"),
            "added_at": _now().isoformat(),
            "known": [a["id"] for a in _releases(artist)],
            "last_checked": None,
            "last_found": None,
            "last_error": None,
        }
        self._save()
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

    async def _check_artist(self, entry: dict) -> None:
        entry["last_checked"] = _now().isoformat()
        try:
            artist = await browser.artist(entry["id"], fresh=True)
        except BrowseError as exc:
            entry["last_error"] = str(exc)
            return
        entry["last_error"] = None
        entry["name"] = artist["name"]

        known = set(entry["known"])
        for album in _releases(artist):
            if album["id"] in known or not _is_released(album):
                continue
            label = f"{album['title']} by {album['artist']}"
            manager.enqueue(
                "id",
                f"[Monitor] {label}",
                {"source": "qobuz", "media_type": "album", "id": album["id"], "desc": label},
            )
            entry["known"].append(album["id"])
            entry["last_found"] = {"title": album["title"], "at": _now().isoformat()}

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
