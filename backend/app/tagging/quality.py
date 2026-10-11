"""Compares audio quality between an existing library file and a freshly
staged one, so a move never silently downgrades a track (e.g. a hi-res FLAC
replaced by a 16-bit one, or a lossy file replacing a lossless one)."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4


@dataclass
class QualityInfo:
    ext: str
    lossless: bool
    bit_depth: Optional[int] = None
    sample_rate: Optional[int] = None
    bitrate: Optional[int] = None

    def describe(self) -> str:
        label = self.ext.lstrip(".").upper()
        if self.lossless and self.bit_depth and self.sample_rate:
            return f"{label} {self.bit_depth}-bit/{self.sample_rate / 1000:.1f}kHz"
        if self.bitrate:
            return f"{label} {self.bitrate // 1000}kbps"
        return label


def read_quality(path: str) -> Optional[QualityInfo]:
    ext = Path(path).suffix.lower()
    try:
        if ext == ".flac":
            audio = FLAC(path)
            return QualityInfo(
                ext=ext, lossless=True,
                bit_depth=audio.info.bits_per_sample, sample_rate=audio.info.sample_rate,
            )
        if ext == ".mp3":
            audio = MP3(path)
            return QualityInfo(ext=ext, lossless=False, bitrate=audio.info.bitrate)
        if ext == ".m4a":
            audio = MP4(path)
            codec = (getattr(audio.info, "codec", "") or "").lower()
            if codec.startswith("alac"):
                return QualityInfo(
                    ext=ext, lossless=True,
                    bit_depth=getattr(audio.info, "bits_per_sample", None) or 16,
                    sample_rate=audio.info.sample_rate,
                )
            return QualityInfo(ext=ext, lossless=False, bitrate=getattr(audio.info, "bitrate", None))
    except Exception:
        return None
    return None


def new_is_lower_quality(existing: QualityInfo, new: QualityInfo) -> bool:
    """True if `new` is strictly lower quality than `existing` — caller should
    skip the overwrite. Lossless always beats lossy regardless of bitrate
    numbers; within the same losslessness, compares bit depth * sample rate
    (FLAC/ALAC) or bitrate (lossy)."""
    if existing.lossless and not new.lossless:
        return True
    if not existing.lossless and new.lossless:
        return False
    if existing.lossless and new.lossless:
        existing_metric = (existing.bit_depth or 0) * (existing.sample_rate or 0)
        new_metric = (new.bit_depth or 0) * (new.sample_rate or 0)
        return new_metric < existing_metric
    return (new.bitrate or 0) < (existing.bitrate or 0)
