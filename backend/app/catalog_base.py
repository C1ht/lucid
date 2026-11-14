"""Shared pieces for the reference catalogues (MusicBrainz, Spotify)."""

import difflib
import re
import unicodedata
from typing import Optional


class CatalogError(Exception):
    """A reference catalogue couldn't answer; the message is shown to the user."""


MATCH_THRESHOLD = 0.86

_PAREN = re.compile(r"[\(\[][^)\]]*[\)\]]")
_SUFFIX = re.compile(r"\s+-\s+.*$")
_FEAT = re.compile(r"\b(feat|ft|featuring|with)\b.*$")


def norm(text: Optional[str]) -> str:
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = _PAREN.sub(" ", s)
    s = _SUFFIX.sub("", s)  # "Song - Remastered 2011", "Song - Radio Edit"
    s = s.replace("&", " and ")
    s = _FEAT.sub("", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def sim(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()
