"""Builds Navidrome-correct tag values from raw Qobuz API responses (the kind
returned by QobuzClient.get_metadata(id, "album"|"track") — see
docs/navidrome-tagging.md for the full field-by-field mapping and why each
decision was made).

Every tag value is a list[str], even for singular tags, so the writer module
has one uniform shape to serialize per audio format.
"""

import re
from dataclasses import dataclass, field
from typing import Optional

from .roles import resolve_credits

_GENRE_SPLIT = re.compile(r"([^→/]+)")
_REISSUE_KEYWORDS = ("remaster", "deluxe", "anniversary", "reissue", "edition")


@dataclass
class TrackTagPlan:
    disc_number: int
    track_number: int
    tags: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class AlbumTagPlan:
    tracks: list[TrackTagPlan] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _dedup(values: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for v in values:
        seen.setdefault(v, None)
    return list(seen.keys())


def _parse_genres(genres_list: list[str]) -> list[str]:
    pieces = []
    for g in genres_list or []:
        pieces.extend(m.strip() for m in _GENRE_SPLIT.findall(g) if m.strip())
    return _dedup(pieces)


def _album_artist_fields(resp: dict) -> tuple[list[str], str, bool]:
    """Returns (albumartists_plural_or_empty, albumartist_display, is_compilation)."""
    artists = resp.get("artists") or []
    names = _dedup([a["name"] for a in artists if a.get("name")])
    if not names:
        single = ((resp.get("artist") or {}).get("name")) or "Unknown Artist"
        return [], single, single.strip().lower() == "various artists"
    if len(names) == 1:
        return [], names[0], names[0].strip().lower() == "various artists"
    return names, " & ".join(names), False


def _resolve_dates(resp: dict) -> tuple[Optional[str], Optional[str]]:
    """Returns (releasedate, originaldate_or_None). ORIGINALDATE is only set
    when the date fields actually diverge — never inferred from a title."""
    original = resp.get("release_date_original")
    download = resp.get("release_date_download")
    stream = resp.get("release_date_stream")
    releasedate = original or download or stream

    others = {d for d in (download, stream) if d}
    if original and others and others != {original}:
        candidates = [d for d in (original, download, stream) if d]
        return releasedate, min(candidates)
    return releasedate, None


def _is_likely_reissue_title(title: str) -> bool:
    lowered = title.lower()
    return any(kw in lowered for kw in _REISSUE_KEYWORDS)


def _artist_display(main: list[str], featured: list[str]) -> str:
    display = " & ".join(main) if main else ""
    if featured:
        display = f"{display} feat. {' & '.join(featured)}" if display else " & ".join(featured)
    return display


@dataclass
class _AlbumContext:
    album_title: str
    albumartists: list[str]
    albumartist: str
    is_compilation: bool
    genres: list[str]
    label: Optional[str]
    barcode: Optional[str]
    album_composer: Optional[str]
    releasedate: Optional[str]
    originaldate: Optional[str]
    tracktotal: Optional[int]
    disctotal: int
    allow_multivalue: bool
    allow_roles: bool
    allow_performer: bool


def _album_context(resp: dict, version_tier: str, disctotal_hint: Optional[int] = None) -> _AlbumContext:
    albumartists, albumartist, is_compilation = _album_artist_fields(resp)
    releasedate, originaldate = _resolve_dates(resp)
    label_field = resp.get("label")
    label = label_field.get("name") if isinstance(label_field, dict) else label_field

    items = ((resp.get("tracks") or {}).get("items")) or []
    tracktotal = resp.get("tracks_count") or (len(items) or None)
    disc_numbers = [t.get("media_number", 1) for t in items]
    disctotal = max(disc_numbers) if disc_numbers else (disctotal_hint or 1)

    return _AlbumContext(
        album_title=resp.get("title") or "Unknown Album",
        albumartists=albumartists,
        albumartist=albumartist,
        is_compilation=is_compilation,
        genres=_parse_genres(resp.get("genres_list") or resp.get("genre") or []),
        label=label,
        barcode=resp.get("upc"),
        album_composer=(resp.get("composer") or {}).get("name"),
        releasedate=releasedate,
        originaldate=originaldate,
        tracktotal=tracktotal,
        disctotal=disctotal,
        allow_multivalue=version_tier != "pre_0_45",
        allow_roles=version_tier in ("0_51_to_0_54", "0_55_plus"),
        allow_performer=version_tier == "0_55_plus",
    )


def _build_track_tags(item: dict, ctx: _AlbumContext) -> TrackTagPlan:
    disc_number = item.get("media_number", 1)
    track_number = item.get("track_number", 1)
    tags: dict[str, list[str]] = {}
    notes: list[str] = []

    tags["ALBUM"] = [ctx.album_title]
    tags["TRACKNUMBER"] = [str(track_number)]
    tags["DISCNUMBER"] = [str(disc_number)]
    if ctx.tracktotal:
        tags["TRACKTOTAL"] = [str(ctx.tracktotal)]
    tags["DISCTOTAL"] = [str(ctx.disctotal)]
    if ctx.releasedate:
        tags["RELEASEDATE"] = [ctx.releasedate]
    if ctx.originaldate:
        tags["ORIGINALDATE"] = [ctx.originaldate]
    if ctx.label:
        tags["LABEL"] = [ctx.label]
    if ctx.barcode:
        tags["BARCODE"] = [str(ctx.barcode)]
    if ctx.album_composer:
        tags["ALBUMCOMPOSER"] = [ctx.album_composer]
    if ctx.genres:
        tags["GENRE"] = ctx.genres if ctx.allow_multivalue else ctx.genres[:1]

    if ctx.is_compilation:
        tags["ALBUMARTIST"] = ["Various Artists"]
        tags["COMPILATION"] = ["1"]
    elif ctx.albumartists and ctx.allow_multivalue:
        tags["ALBUMARTISTS"] = ctx.albumartists
        tags["ALBUMARTIST"] = [ctx.albumartist]
    else:
        tags["ALBUMARTIST"] = [ctx.albumartist]

    isrc = item.get("isrc")
    if isrc:
        tags["ISRC"] = [isrc]

    performers = item.get("performers") or ""
    credits = resolve_credits(performers) if performers else None

    if credits is not None and credits.unparseable:
        notes.append(
            f"Track {disc_number}.{track_number}: performers field wasn't clean "
            "structured data (looks like hand-written liner notes) — skipped role/credit "
            "extraction for this track rather than guessing."
        )
        credits = None

    all_artists = (credits.main_artists + credits.featured_artists) if credits else []
    if all_artists:
        tags["ARTIST"] = [_artist_display(credits.main_artists, credits.featured_artists)]
        if ctx.allow_multivalue and len(all_artists) > 1:
            tags["ARTISTS"] = all_artists
    else:
        # No usable performers data (missing or unparseable) — fall back to
        # the track's separate, always-clean single `performer` field.
        fallback_name = (item.get("performer") or {}).get("name")
        if fallback_name:
            tags["ARTIST"] = [fallback_name]

    if credits is not None:
        if credits.role_tags:
            if ctx.allow_roles:
                for tag, names in credits.role_tags.items():
                    tags[tag] = names if ctx.allow_multivalue else names[:1]
            else:
                summary = ", ".join(f"{tag}={names}" for tag, names in credits.role_tags.items())
                notes.append(
                    f"Track {disc_number}.{track_number}: role tags need Navidrome "
                    f"0.51.1+, dropped: {summary}"
                )

        if credits.performer_pairs:
            if ctx.allow_performer:
                tags["PERFORMER"] = [f"{name} ({role})" for name, role in credits.performer_pairs]
            else:
                summary = ", ".join(f"{name} ({role})" for name, role in credits.performer_pairs)
                notes.append(
                    f"Track {disc_number}.{track_number}: PERFORMER sub-roles need "
                    f"Navidrome 0.55.0+, dropped: {summary}"
                )

        for name, raw_role in credits.dropped:
            notes.append(
                f"Track {disc_number}.{track_number}: dropped role '{raw_role}' for "
                f"{name} (not a credit Navidrome models)"
            )

        for name in credits.writer_fallbacks:
            notes.append(
                f"Track {disc_number}.{track_number}: 'Writer' credit for {name} mapped "
                "to COMPOSER (no ComposerLyricist entry present on this track)"
            )

    return TrackTagPlan(disc_number=disc_number, track_number=track_number, tags=tags, notes=notes)


def build_album_tag_plan(resp: dict, version_tier: str) -> AlbumTagPlan:
    """resp: raw response from get_metadata(album_id, "album")."""
    plan = AlbumTagPlan()
    ctx = _album_context(resp, version_tier)

    if ctx.originaldate is None and _is_likely_reissue_title(ctx.album_title):
        plan.notes.append(
            f"Album '{ctx.album_title}' looks like a reissue by its title, but Qobuz's "
            "release dates don't actually differ — not writing ORIGINALDATE (would be a guess)."
        )
    if version_tier == "pre_0_45":
        plan.notes.append(
            "Navidrome version tier is set to pre-0.45.0: that version can't read "
            "multi-valued tags, role tags, or PERFORMER sub-roles. Only basic single-value "
            "tags (ARTIST, ALBUM, dates, etc.) were written for this album."
        )

    items = ((resp.get("tracks") or {}).get("items")) or []
    for item in items:
        plan.tracks.append(_build_track_tags(item, ctx))

    return plan


def build_track_tag_plan(resp: dict, version_tier: str) -> TrackTagPlan:
    """resp: raw response from get_metadata(track_id, "track") — a single-track
    download. The nested resp["album"] carries the album-level fields."""
    album_resp = resp.get("album") or {}
    ctx = _album_context(album_resp, version_tier, disctotal_hint=resp.get("media_number", 1))
    return _build_track_tags(resp, ctx)
