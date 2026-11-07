"""User-chosen tag overrides ("make this show up as Artist / Album, with this
cover"), applied to the freshly downloaded files in staging, after the normal
Qobuz retag, so they always win.

Works for every format a source can hand us: FLAC and Opus/Ogg (Vorbis
comments), MP3 (ID3) and M4A (MP4 atoms). The files are then moved to
`Artist/Album/Title.ext`, because Navidrome reads tags but people browse by
folder too, and the folder streamrip or yt-dlp picked would still say the
original uploader.
"""

import base64
import io
import re
import shutil
import uuid
from pathlib import Path
from typing import Optional

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from PIL import Image

from .. import paths
from ..schemas import CustomTags
from .staging import iter_audio_files

COVER_MAX_PX = 1400
_COVER_NAMES = ("cover", "folder")
_IMAGE_EXTS = {".jpg", ".jpeg", ".png"}


def save_cover(data: bytes) -> str:
    """Normalises an uploaded image to a JPEG (what every format embeds
    reliably) and returns the id it's stored under."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:  # noqa: BLE001 - Pillow raises many types for bad input
        raise ValueError("That file isn't an image Lucid can read") from exc
    img = img.convert("RGB")
    img.thumbnail((COVER_MAX_PX, COVER_MAX_PX))
    paths.COVERS_DIR.mkdir(parents=True, exist_ok=True)
    cover_id = uuid.uuid4().hex
    img.save(paths.COVERS_DIR / f"{cover_id}.jpg", "JPEG", quality=92)
    return cover_id


def load_cover(cover_id: str) -> Optional[bytes]:
    path = paths.COVERS_DIR / f"{cover_id}.jpg"
    return path.read_bytes() if path.is_file() else None


def _picture(data: bytes) -> Picture:
    img = Image.open(io.BytesIO(data))
    pic = Picture()
    pic.type, pic.mime, pic.data = 3, "image/jpeg", data
    pic.width, pic.height, pic.depth = img.width, img.height, 24
    return pic


def _write_vorbis(audio, fields: dict, cover: Optional[bytes]) -> None:
    names = {
        "title": "title", "artist": "artist", "album_artist": "albumartist",
        "album": "album", "genre": "genre", "year": "date",
    }
    for key, tag in names.items():
        if fields.get(key):
            audio[tag] = [fields[key]]
    if fields.get("track_number"):
        audio["tracknumber"] = [str(fields["track_number"])]
    # Qobuz-retagged files carry multi-value ARTISTS fields, which Navidrome
    # prefers over ARTIST, so an old value there would undo the override.
    if fields.get("artist") and "artists" in audio:
        del audio["artists"]
    if fields.get("album_artist") and "albumartists" in audio:
        del audio["albumartists"]
    if cover:
        pic = _picture(cover)
        if isinstance(audio, FLAC):
            audio.clear_pictures()
            audio.add_picture(pic)
        else:
            audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
    audio.save()


def _write_mp3(path: str, fields: dict, cover: Optional[bytes]) -> None:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    frames = {"title": TIT2, "artist": TPE1, "album_artist": TPE2, "album": TALB, "genre": TCON, "year": TDRC}
    for key, frame in frames.items():
        if fields.get(key):
            tags.setall(frame.__name__, [frame(encoding=3, text=[fields[key]])])
    if fields.get("track_number"):
        tags.setall("TRCK", [TRCK(encoding=3, text=[str(fields["track_number"])])])
    if cover:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
    tags.save(path)


def _write_m4a(path: str, fields: dict, cover: Optional[bytes]) -> None:
    audio = MP4(path)
    atoms = {
        "title": "\xa9nam", "artist": "\xa9ART", "album_artist": "aART",
        "album": "\xa9alb", "genre": "\xa9gen", "year": "\xa9day",
    }
    for key, atom in atoms.items():
        if fields.get(key):
            audio[atom] = [fields[key]]
    if fields.get("track_number"):
        audio["trkn"] = [(fields["track_number"], 0)]
    if cover:
        audio["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()


def _write(path: Path, fields: dict, cover: Optional[bytes]) -> None:
    ext = path.suffix.lower()
    if ext == ".flac":
        _write_vorbis(FLAC(str(path)), fields, cover)
    elif ext == ".opus":
        _write_vorbis(OggOpus(str(path)), fields, cover)
    elif ext == ".ogg":
        _write_vorbis(OggVorbis(str(path)), fields, cover)
    elif ext == ".mp3":
        _write_mp3(str(path), fields, cover)
    elif ext == ".m4a":
        _write_m4a(str(path), fields, cover)
    else:
        raise ValueError(f"'{ext}' files can't be tagged")


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "Unknown"


def _first(easy, key: str) -> str:
    values = (easy or {}).get(key) or []
    return str(values[0]).strip() if values else ""


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    n = 2
    while True:
        candidate = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def apply_custom_tags(staging_dir: Path, tags: CustomTags) -> list[str]:
    """Returns log lines. Raises only for things that apply to the whole job
    (a missing cover); a single unreadable file is reported and skipped."""
    notes: list[str] = []
    files = iter_audio_files(staging_dir)
    if not files:
        return ["No audio files found to tag."]

    fields = {k: v for k, v in tags.model_dump(exclude={"cover_id"}).items() if v is not None}
    if len(files) > 1:
        # One title or track number on a whole album would be wrong.
        for key in ("title", "track_number"):
            if fields.pop(key, None) is not None:
                notes.append(f"Several files were downloaded, so the {key.replace('_', ' ')} was left alone.")

    cover: Optional[bytes] = None
    if tags.cover_id:
        cover = load_cover(tags.cover_id)
        if cover is None:
            notes.append("The chosen cover image is no longer on the server, keeping the original cover.")

    tagged = 0
    for path in files:
        try:
            _write(path, fields, cover)
            easy = mutagen.File(str(path), easy=True)
            artist = _first(easy, "artist")
            album_artist = _first(easy, "albumartist") or artist
            title = _first(easy, "title") or path.stem
            album = _first(easy, "album")
            if not album:
                # Navidrome files tagless albums under "[Unknown Album]".
                album = title
                _write(path, {"album": album}, None)
            track = _first(easy, "tracknumber").split("/")[0]
            name = f"{int(track):02d}. {title}" if track.isdigit() else title
            dest_dir = staging_dir / _safe(album_artist or "Unknown Artist") / _safe(album)
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = _unique(dest_dir / f"{_safe(name)}{path.suffix.lower()}")
            old_dir = path.parent
            if dest != path:
                shutil.move(str(path), str(dest))
            _carry_cover_art(old_dir, dest_dir)
            if cover:
                (dest_dir / "cover.jpg").write_bytes(cover)
            tagged += 1
        except Exception as exc:  # noqa: BLE001 - one bad file shouldn't sink the rest
            notes.append(f"{path.name}: couldn't apply custom tags ({exc}), left as downloaded.")

    _prune_empty(staging_dir)
    changed = ", ".join(sorted(k.replace("_", " ") for k in fields)) + (", cover" if cover else "")
    notes.append(f"Applied custom tags ({changed}) to {tagged} file(s).")
    return notes


def _carry_cover_art(old_dir: Path, new_dir: Path) -> None:
    """The folder cover streamrip wrote next to a track follows it."""
    if old_dir == new_dir or not old_dir.is_dir():
        return
    for img in old_dir.iterdir():
        if img.is_file() and img.suffix.lower() in _IMAGE_EXTS and img.stem.lower() in _COVER_NAMES:
            target = new_dir / img.name
            if not target.exists():
                shutil.copy2(img, target)


def _prune_empty(root: Path) -> None:
    """Drop what's left of the original folders: copied covers' sources and empty dirs."""
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(f.suffix.lower() in {e for e in (".flac", ".mp3", ".m4a", ".opus", ".ogg")} for f in d.iterdir() if f.is_file()):
            for f in list(d.iterdir()):
                if f.is_file() and f.suffix.lower() in _IMAGE_EXTS and f.stem.lower() in _COVER_NAMES:
                    f.unlink()
            if not any(d.iterdir()):
                d.rmdir()
