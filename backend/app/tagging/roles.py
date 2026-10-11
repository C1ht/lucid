"""Parses Qobuz's `performers` credit string into structured (name, roles) pairs
and resolves each role to a Navidrome tag, per the mapping table confirmed against
real API responses (see docs/navidrome-tagging.md)."""

from dataclasses import dataclass, field

# Direct 1:1 role -> Navidrome tag, confirmed to exist as native keys in
# navidrome's resources/mappings.yaml.
_DIRECT_ROLE_TAGS = {
    "producer": "PRODUCER",
    "composer": "COMPOSER",
    "lyricist": "LYRICIST",
    "arranger": "ARRANGER",
    "conductor": "CONDUCTOR",
    "remixer": "REMIXER",
    "djmixer": "DJMIXER",
    "director": "DIRECTOR",
}

_MAIN_ARTIST = "mainartist"
_FEATURED_ARTIST = "featuredartist"
_COMPOSER_LYRICIST = "composerlyricist"
_WRITER = "writer"

# Known-but-not-a-performance-credit roles: not a Navidrome tag, dropped with
# a logged reason rather than silently or via a guess.
_DROPPED_ROLES = {"associatedperformer", "musicpublisher"}


def _normalize(role: str) -> str:
    return role.lower().replace(" ", "").replace("-", "")


def parse_performers(performers: str) -> list[tuple[str, list[str]]]:
    """'Name, Role1, Role2 - Name2, Role' -> [(name, [role, ...]), ...]."""
    if not performers:
        return []
    entries = performers.split(" - ")
    result = []
    for entry in entries:
        parts = [p.strip() for p in entry.split(",") if p.strip()]
        if not parts:
            continue
        name, roles = parts[0], parts[1:]
        result.append((name, roles))
    return result


def _is_sane(entries: list[tuple[str, list[str]]]) -> bool:
    """Qobuz's `performers` field is usually clean machine-generated
    "Name, Role - Name, Role" text, but some deep-catalog/reissue albums carry
    hand-written liner-notes prose instead (nested parens, "Role by Name",
    "Role : Name1, Name2" groupings). Splitting that naively produces garbage
    fragments. Rather than guess, detect the telltale signs and bail out."""
    for name, roles in entries:
        if ":" in name or any(":" in r for r in roles):
            return False
        # A split that cuts through a parenthetical leaves an unbalanced
        # fragment on one or both sides of the cut.
        fields = [name, *roles]
        if any(f.count("(") != f.count(")") for f in fields):
            return False
    return True


@dataclass
class ParsedCredits:
    main_artists: list[str] = field(default_factory=list)
    featured_artists: list[str] = field(default_factory=list)
    # Navidrome tag name -> ordered, deduped list of credited names
    role_tags: dict[str, list[str]] = field(default_factory=dict)
    # (name, sub-role text) pairs for the generic PERFORMER(role) tag
    performer_pairs: list[tuple[str, str]] = field(default_factory=list)
    # (name, raw role string) dropped because the role isn't a credit Navidrome models
    dropped: list[tuple[str, str]] = field(default_factory=list)
    # names where the Writer -> COMPOSER fallback fired (per your ask: always log these)
    writer_fallbacks: list[str] = field(default_factory=list)
    # True if the performers string didn't look like clean structured data and
    # was rejected rather than parsed into something unreliable.
    unparseable: bool = False


def resolve_credits(performers: str) -> ParsedCredits:
    entries = parse_performers(performers)
    if not _is_sane(entries):
        return ParsedCredits(unparseable=True)

    credits = ParsedCredits()

    has_composer_lyricist = any(
        _normalize(role) == _COMPOSER_LYRICIST for _, roles in entries for role in roles
    )

    def add_role(tag: str, name: str) -> None:
        names = credits.role_tags.setdefault(tag, [])
        if name not in names:
            names.append(name)

    for name, roles in entries:
        for raw_role in roles:
            norm = _normalize(raw_role)

            if norm == _MAIN_ARTIST:
                if name not in credits.main_artists:
                    credits.main_artists.append(name)
            elif norm == _FEATURED_ARTIST:
                if name not in credits.featured_artists:
                    credits.featured_artists.append(name)
            elif norm == _COMPOSER_LYRICIST:
                add_role("COMPOSER", name)
                add_role("LYRICIST", name)
            elif norm == _WRITER:
                # Ambiguous between composer/lyricist. Only used as a fallback
                # when this track has no clearer ComposerLyricist credit.
                if has_composer_lyricist:
                    continue
                add_role("COMPOSER", name)
                credits.writer_fallbacks.append(name)
            elif norm in _DIRECT_ROLE_TAGS:
                add_role(_DIRECT_ROLE_TAGS[norm], name)
            elif "mix" in norm:
                add_role("MIXER", name)
            elif "engineer" in norm:
                add_role("ENGINEER", name)
            elif norm in _DROPPED_ROLES:
                credits.dropped.append((name, raw_role))
            else:
                # Catch-all: instrument/vocal/misc credit -> PERFORMER sub-role.
                credits.performer_pairs.append((name, raw_role))

    return credits
