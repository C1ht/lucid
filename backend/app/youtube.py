"""YouTube support, built on yt-dlp (streamrip 2.1.0 has a [youtube] config
section but no YouTube client, so `rip` can't download from it).

Downloads keep the stream YouTube serves: yt-dlp pulls the best audio-only
stream and only re-wraps it (webm/Opus -> .opus, m4a stays .m4a) with no
re-encode, so the file is exactly what YouTube delivered. YouTube audio is
lossy (about 128-160 kbps Opus, or AAC); converting it to FLAC would only make
a bigger file with the same sound, so that is deliberately never done here.
"""

import asyncio
import re
import sys
import time
from typing import Any, Optional

from . import paths
from .streamrip_cli import LineCallback, run_streaming

COOKIES_PATH = paths.CONFIG_DIR / "youtube-cookies.txt"
SEARCH_LIMIT = 20
CACHE_TTL_SECONDS = 300

_URL_RE = re.compile(
    r"^https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be|youtube-nocookie\.com)/", re.I
)
_cache: dict[str, tuple[float, list[dict]]] = {}


class YoutubeError(Exception):
    pass


def is_youtube_url(url: str) -> bool:
    return bool(_URL_RE.match(url.strip()))


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _common_args() -> list[str]:
    args = ["--no-colors", "--no-warnings", "--no-mtime"]
    if COOKIES_PATH.exists():
        args += ["--cookies", str(COOKIES_PATH)]
    return args


def _search_blocking(query: str) -> list[dict]:
    import yt_dlp

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "socket_timeout": 20,
    }
    if COOKIES_PATH.exists():
        opts["cookiefile"] = str(COOKIES_PATH)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{SEARCH_LIMIT}:{query}", download=False)
    except Exception as exc:  # noqa: BLE001 - yt-dlp raises many types
        raise YoutubeError(f"YouTube search failed: {exc}") from exc

    results = []
    for e in (info or {}).get("entries") or []:
        vid = e.get("id")
        if not vid:
            continue
        thumbs = e.get("thumbnails") or []
        results.append(
            {
                "id": vid,
                "title": e.get("title") or "Untitled",
                "channel": e.get("channel") or e.get("uploader") or "",
                "duration": int(e["duration"]) if e.get("duration") else None,
                "thumbnail": thumbs[-1].get("url") if thumbs else f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            }
        )
    return results


async def search(query: str) -> list[dict]:
    key = query.strip().lower()
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    results = await asyncio.to_thread(_search_blocking, query.strip())
    _cache[key] = (time.monotonic(), results)
    if len(_cache) > 100:
        for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:30]:
            _cache.pop(k, None)
    return results


async def download(url: str, staging_dir, on_line: Optional[LineCallback]) -> int:
    """Download `url` (a video, or a playlist) into staging_dir as audio files."""
    args = [
        sys.executable, "-m", "yt_dlp",
        *_common_args(),
        "-f", "bestaudio/best",
        # "best" = keep the original codec: remux only, never transcode.
        "-x", "--audio-format", "best",
        "--embed-metadata", "--embed-thumbnail", "--convert-thumbnails", "jpg",
        "--windows-filenames",
        "--no-progress",  # the job's byte counter already tracks progress
        "-o", str(staging_dir) + "/%(album_artist,artist,channel,uploader)s/%(title)s [%(id)s].%(ext)s",
        "--no-playlist",  # a watch link that also has list= means just that video
        url,
    ]

    return await run_streaming(args, on_line)
