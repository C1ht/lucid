"""Builds Navidrome-correct tag values from Deezer's public API data (an album with its tracks'
own details, or a single track), producing the same plans as the other mappers.

Deezer gives per-track contributors with a role (Main / Featured), ISRC, label, genres and the
album barcode, so artists are split properly and genre and label are written too. It has no role
credits (producer, composer...), so those aren't written.
"""

from typing import Optional

from .qobuz_mapper import AlbumTagPlan, TrackTagPlan, _artist_display, _dedup


def _people(item: dict) -> tuple[list[str], list[str]]:
    people = item.get("contributors") or ([item["artist"]] if item.get("artist") else [])
    main = _dedup([p["name"] for p in people if p.get("name") and (p.get("role") or "Main") == "Main"])
    featured = _dedup([p["name"] for p in people if p.get("name") and p.get("role") == "Featured"])
    if not main and featured:
        main, featured = featured, []
    return main, featured


def _album_ctx(album: dict, tracks: list[dict], version_tier: str) -> dict:
    main, _ = _people(album)
    genres = [g["name"] for g in ((album.get("genres") or {}).get("data") or []) if g.get("name")]
    discs = [t.get("disk_number") or 1 for t in tracks]
    return {
        "title": album.get("title") or "Unknown Album",
        "main": main,
        "is_va": bool(main) and main[0].strip().lower() == "various artists",
        "date": album.get("release_date") or None,
        "barcode": album.get("upc"),
        "label": album.get("label"),
        "genres": genres,
        "tracktotal": album.get("nb_tracks") or (len(tracks) or None),
        "disctotal": max(discs) if discs else 1,
        "multi": version_tier != "pre_0_45",
    }


def _track_tags(item: dict, ctx: dict, notes: Optional[list[str]] = None) -> TrackTagPlan:
    disc = item.get("disk_number") or 1
    number = item.get("track_position") or 1
    tags: dict[str, list[str]] = {
        "ALBUM": [ctx["title"]],
        "TRACKNUMBER": [str(number)],
        "DISCNUMBER": [str(disc)],
        "DISCTOTAL": [str(ctx["disctotal"])],
    }
    if ctx["tracktotal"]:
        tags["TRACKTOTAL"] = [str(ctx["tracktotal"])]
    if ctx["date"]:
        tags["RELEASEDATE"] = [ctx["date"]]
    if ctx["barcode"]:
        tags["BARCODE"] = [str(ctx["barcode"])]
    if ctx["label"]:
        tags["LABEL"] = [ctx["label"]]
    if ctx["genres"]:
        tags["GENRE"] = ctx["genres"] if ctx["multi"] else ctx["genres"][:1]

    main = ctx["main"]
    if ctx["is_va"]:
        tags["ALBUMARTIST"] = ["Various Artists"]
        tags["COMPILATION"] = ["1"]
    elif len(main) > 1 and ctx["multi"]:
        tags["ALBUMARTISTS"] = main
        tags["ALBUMARTIST"] = [" & ".join(main)]
    elif main:
        tags["ALBUMARTIST"] = [" & ".join(main)]

    if item.get("isrc"):
        tags["ISRC"] = [item["isrc"]]

    t_main, t_feat = _people(item)
    everyone = t_main + t_feat
    if everyone:
        tags["ARTIST"] = [_artist_display(t_main, t_feat)]
        if ctx["multi"] and len(everyone) > 1:
            tags["ARTISTS"] = everyone
    return TrackTagPlan(disc_number=disc, track_number=number, tags=tags, notes=list(notes or []))


def build_album_tag_plan(album: dict, version_tier: str) -> AlbumTagPlan:
    """album: full_album() output (its `tracks` carry per-track details)."""
    tracks = album.get("tracks") or []
    ctx = _album_ctx(album, tracks, version_tier)
    plan = AlbumTagPlan()
    if version_tier == "pre_0_45":
        plan.notes.append("Navidrome version tier is set to pre-0.45.0: only basic single-value tags were written for this album.")
    for t in tracks:
        plan.tracks.append(_track_tags(t, ctx))
    return plan


def build_track_tag_plan(track: dict, album: Optional[dict], version_tier: str) -> TrackTagPlan:
    full = {**(track.get("album") or {}), **(album or {})}
    ctx = _album_ctx(full, [track], version_tier)
    if not ctx["main"]:
        ctx["main"], _ = _people(track)
    notes = [] if album else ["Couldn't fetch the album's details, so release date, label and genre are left as streamrip wrote them."]
    return _track_tags(track, ctx, notes)
