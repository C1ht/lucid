"""Builds Navidrome-correct tag values from raw Tidal API responses (what streamrip's
TidalClient.get_metadata returns for an album or a track), the same way qobuz_mapper does for
Qobuz, producing the same plans so retag.py can write them.

Tidal gives less than Qobuz: no label, genre or role credits in these responses, so those tags are
simply left as streamrip wrote them. What it does give (artists split into main and featured,
barcode, release date, track and disc totals, ISRC) is mapped so Navidrome groups albums and
splits artists correctly.
"""

from typing import Optional

from .qobuz_mapper import AlbumTagPlan, TrackTagPlan, _artist_display, _dedup


def _people(item: dict) -> tuple[list[str], list[str]]:
    """(main artists, featured artists) of an album or track."""
    people = item.get("artists") or ([item["artist"]] if item.get("artist") else [])
    main = _dedup([a["name"] for a in people if a.get("name") and (a.get("type") or "MAIN") == "MAIN"])
    featured = _dedup([a["name"] for a in people if a.get("name") and a.get("type") == "FEATURED"])
    if not main and featured:  # nothing marked MAIN: don't lose the credit
        main, featured = featured, []
    return main, featured


def _title(item: dict) -> str:
    title = item.get("title") or "Unknown Album"
    version = item.get("version")
    return f"{title} ({version})" if version and version.lower() not in title.lower() else title


def _album_tags(album: dict, version_tier: str) -> dict:
    main, _ = _people(album)
    is_va = bool(main) and main[0].strip().lower() == "various artists"
    return {
        "title": _title(album),
        "main": main,
        "is_va": is_va,
        "date": album.get("releaseDate") or (album.get("streamStartDate") or "")[:10] or None,
        "barcode": album.get("upc"),
        "tracktotal": album.get("numberOfTracks"),
        "disctotal": album.get("numberOfVolumes") or 1,
        "multi": version_tier != "pre_0_45",
    }


def _track_tags(item: dict, ctx: dict, notes: Optional[list[str]] = None) -> TrackTagPlan:
    disc = item.get("volumeNumber") or 1
    number = item.get("trackNumber") or 1
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


def build_album_tag_plan(resp: dict, version_tier: str) -> AlbumTagPlan:
    """resp: get_metadata(album_id, "album"); its `tracks` is a list of track dicts."""
    ctx = _album_tags(resp, version_tier)
    plan = AlbumTagPlan()
    if version_tier == "pre_0_45":
        plan.notes.append(
            "Navidrome version tier is set to pre-0.45.0: only basic single-value tags were written for this album."
        )
    for item in resp.get("tracks") or []:
        plan.tracks.append(_track_tags(item, ctx))
    return plan


def build_track_tag_plan(resp: dict, album_resp: Optional[dict], version_tier: str) -> TrackTagPlan:
    """resp: get_metadata(track_id, "track"). The track only carries a stub of its album, so the
    full album response (release date, barcode, totals) is passed in when it could be fetched."""
    album = {**(resp.get("album") or {}), **(album_resp or {})}
    if not album.get("artists") and not album.get("artist"):
        album["artists"] = resp.get("artists") or []
        album["artist"] = resp.get("artist")
    ctx = _album_tags(album, version_tier)
    notes = [] if album_resp else ["Couldn't fetch the album's details, so release date and barcode are left as streamrip wrote them."]
    return _track_tags(resp, ctx, notes)
