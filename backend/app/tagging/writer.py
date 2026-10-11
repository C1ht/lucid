"""Applies a per-track tag plan (dict[str, list[str]]) to a FLAC file with
mutagen. Vorbis comments natively allow arbitrary multi-valued fields, so
ARTIST/ARTISTS can coexist as separate fields exactly as Navidrome expects.

Other formats (MP3/ID3, MP4/M4A) are deliberately NOT retagged here: ID3v2.4
only has one frame per concept (e.g. TPE1), so the Vorbis trick of a separate
singular "display" tag alongside a distinct multi-value list isn't directly
possible, and MP4's repeated-atom behavior hasn't been verified against
Navidrome (rule 9's own caution). Callers skip those files and leave them
exactly as streamrip wrote them, logging why."""

from pathlib import Path

from mutagen.flac import FLAC

SUPPORTED_EXTS = {".flac"}


def apply_tags(path: str, tags: dict[str, list[str]]) -> list[str]:
    """Writes `tags` onto the FLAC file at `path`. Returns log notes; never
    raises — any file extension this doesn't support is reported, not retagged."""
    ext = Path(path).suffix.lower()
    if ext != ".flac":
        return [f"{Path(path).name}: '{ext}' tagging not supported yet, left as streamrip wrote it."]

    audio = FLAC(path)
    # streamrip's own tagger already wrote a DATE field (its best guess at a
    # release date). The approved mapping deliberately leaves DATE (recording
    # date) unset since Qobuz gives us no real recording-date data — so clear
    # it rather than silently leaving a stale value that contradicts that.
    if "date" in audio and "DATE" not in tags:
        del audio["date"]
    for key, values in tags.items():
        if not values:
            continue
        audio[key] = values
    audio.save()
    return []
