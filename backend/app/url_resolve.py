"""Resolves a pasted URL to a human-readable label (and, where possible, to
real metadata) instead of showing the raw link in the queue.

The regex below is deliberately the same pattern streamrip's own
`rip/parse_url.py::GenericURL` uses -- duplicated here rather than imported,
since it's internal/unsupported API we don't want a hard import dependency
on, but the pattern itself is just string matching, cheap to keep in sync.
It covers standard Qobuz/Tidal/Deezer URLs without any network call.
SoundCloud links and Deezer's short `deezer.page.link` links need a network
resolve step streamrip itself does internally; those aren't handled here and
fall back to the raw URL as the label.
"""

import re
from typing import Optional

from . import metadata_fetch

_GENERIC_URL_REGEX = re.compile(
    r"https?://(?:www|open|play|listen)?\.?(qobuz|tidal|deezer)\.com"
    r"(?:(?:/(album|artist|track|playlist|video|label))|(?:/[-\w]+?))+/([-\w]+)",
)


def parse_generic_url(url: str) -> Optional[tuple[str, str, str]]:
    """(source, media_type, item_id), or None if this isn't a directly
    parseable Qobuz/Tidal/Deezer link."""
    match = _GENERIC_URL_REGEX.match(url.strip())
    if not match:
        return None
    source, media_type, item_id = match.groups()
    if not source or not media_type or not item_id:
        return None
    return source, media_type, item_id


def _first_str(meta: dict, *keys: str) -> Optional[str]:
    for key in keys:
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _artist_name(meta: dict) -> Optional[str]:
    for key in ("artist", "performer"):
        entry = meta.get(key)
        if isinstance(entry, dict) and entry.get("name"):
            return entry["name"]
    artists = meta.get("artists")
    if isinstance(artists, list) and artists:
        first = artists[0]
        if isinstance(first, dict) and first.get("name"):
            return first["name"]
    user = meta.get("user")
    if isinstance(user, dict) and user.get("username"):
        return user["username"]
    return None


def build_label(media_type: str, meta: dict) -> str:
    title = _first_str(meta, "title", "name") or "Untitled"
    artist = _artist_name(meta)
    if not artist:
        return title
    if media_type == "track":
        return f"{artist} - {title}"
    return f"{title} by {artist}"


async def resolve_url_label(url: str) -> tuple[str, Optional[dict]]:
    """Best-effort (label, raw_metadata_or_None). Never raises -- any
    parsing or network failure just falls back to the raw URL as the label,
    since this is cosmetic and must never block queuing a download."""
    parsed = parse_generic_url(url)
    if parsed is None:
        return url, None
    source, media_type, item_id = parsed
    try:
        meta = await metadata_fetch.fetch_metadata(source, item_id, media_type)
    except Exception:  # noqa: BLE001 - cosmetic only, never block the download
        return url, None
    return build_label(media_type, meta), meta
