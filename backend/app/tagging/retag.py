"""Ties the Qobuz tag mapper and the FLAC writer together: given a staging
directory full of freshly-downloaded audio files and the matching raw Qobuz
metadata, retags every FLAC file in place (other formats are left as
streamrip wrote them — see writer.py).

Files are matched to the metadata by the TRACKNUMBER/DISCNUMBER tags
streamrip's own tagger already wrote correctly — not by filename (fragile
against a customized track_format) or by position (breaks if streamrip
skipped an already-downloaded track)."""

from pathlib import Path
from typing import Optional

from mutagen.flac import FLAC
from mutagen.id3 import ID3, ID3NoHeaderError

from .qobuz_mapper import TrackTagPlan, build_album_tag_plan, build_track_tag_plan
from .staging import iter_audio_files
from .writer import apply_tags


def _read_existing_track_disc(path: Path) -> Optional[tuple[int, int]]:
    ext = path.suffix.lower()
    try:
        if ext == ".flac":
            audio = FLAC(str(path))
            track = (audio.get("tracknumber") or ["1"])[0]
            disc = (audio.get("discnumber") or ["1"])[0]
            return int(str(track).split("/")[0]), int(str(disc).split("/")[0])
        if ext == ".mp3":
            try:
                audio = ID3(str(path))
            except ID3NoHeaderError:
                return None
            trck = audio.get("TRCK")
            tpos = audio.get("TPOS")
            track = str(trck.text[0]).split("/")[0] if trck else "1"
            disc = str(tpos.text[0]).split("/")[0] if tpos else "1"
            return int(track), int(disc)
    except (ValueError, OSError):
        return None
    return None


def _build_summary(track_notes: list[str], retagged_count: int, skipped: list[tuple[str, str]]) -> str:
    writer_fallbacks = sum("'Writer' credit for" in n for n in track_notes)
    sanity_failures = sum("performers field wasn't clean structured data" in n for n in track_notes)
    dropped_roles = sum("dropped role" in n for n in track_notes)
    version_gated = sum("need Navidrome" in n for n in track_notes)

    parts = [f"retagged {retagged_count} file(s)"]
    if skipped:
        by_reason: dict[str, int] = {}
        for _, reason in skipped:
            by_reason[reason] = by_reason.get(reason, 0) + 1
        skip_desc = ", ".join(f"{count} ({reason})" for reason, count in by_reason.items())
        parts.append(f"skipped {len(skipped)}: {skip_desc}")
    if writer_fallbacks:
        parts.append(f"Writer->COMPOSER fallback fired {writer_fallbacks}x")
    if sanity_failures:
        parts.append(f"{sanity_failures} track(s) had an unparseable performers field (used fallback artist)")
    if dropped_roles:
        parts.append(f"{dropped_roles} role credit(s) dropped (not modeled by Navidrome)")
    if version_gated:
        parts.append(f"{version_gated} tag group(s) dropped due to the configured Navidrome version tier")

    return "SUMMARY: " + "; ".join(parts)


def _apply_plan_to_staging(
    staging_dir: Path, track_plans: list[TrackTagPlan]
) -> tuple[list[str], int, list[tuple[str, str]]]:
    notes: list[str] = []
    retagged_count = 0
    skipped: list[tuple[str, str]] = []

    by_key = {(t.disc_number, t.track_number): t for t in track_plans}
    files = iter_audio_files(staging_dir)

    if not files:
        notes.append("No audio files found in the staging directory to retag.")
        return notes, retagged_count, skipped

    for file_path in files:
        key = _read_existing_track_disc(file_path)
        track_plan = by_key.get(key) if key else None

        if track_plan is None:
            reason = f"couldn't match to fetched metadata (read disc/track = {key})"
            notes.append(f"{file_path.name}: {reason} — left exactly as streamrip wrote it.")
            skipped.append((file_path.name, reason))
            continue

        notes.extend(track_plan.notes)
        write_notes = apply_tags(str(file_path), track_plan.tags)
        if write_notes:
            notes.extend(write_notes)
            skipped.append((file_path.name, "format not supported for retagging yet"))
        else:
            retagged_count += 1

    return notes, retagged_count, skipped


def retag_qobuz_album(staging_dir: Path, album_resp: dict, version_tier: str) -> list[str]:
    plan = build_album_tag_plan(album_resp, version_tier)
    track_notes, retagged_count, skipped = _apply_plan_to_staging(staging_dir, plan.tracks)
    notes = list(plan.notes) + track_notes
    notes.append(_build_summary(track_notes, retagged_count, skipped))
    return notes


def retag_qobuz_track(staging_dir: Path, track_resp: dict, version_tier: str) -> list[str]:
    track_plan = build_track_tag_plan(track_resp, version_tier)
    track_notes, retagged_count, skipped = _apply_plan_to_staging(staging_dir, [track_plan])
    notes = list(track_notes)
    notes.append(_build_summary(track_notes, retagged_count, skipped))
    return notes
