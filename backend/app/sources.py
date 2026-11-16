"""Which service the files are downloaded from: Qobuz or Tidal.

The reference catalogue (MusicBrainz and friends) decides what exists; this module knows the
services the files can come from, which one is preferred, and which are usable right now.
"""

from . import config_store
from .browse import browser as qobuz_browser
from .deezer_browse import browser as deezer_browser
from .tidal_browse import browser as tidal_browser

NAMES = {"qobuz": "Qobuz", "tidal": "Tidal", "deezer": "Deezer"}
_BROWSERS = {"qobuz": qobuz_browser, "tidal": tidal_browser, "deezer": deezer_browser}


def ready(name: str) -> bool:
    s = config_store.load_settings()
    if name == "tidal":
        return config_store.tidal_ready()
    if name == "deezer":
        return bool(s.deezer.enabled and s.deezer.arl)
    q = s.qobuz
    return bool(q.enabled and q.email_or_userid and q.password_or_token)


def order() -> list[str]:
    """Sources to try, best first. The preferred one leads, the other follows when the fallback is
    on and it is set up. A preferred source that isn't set up is skipped if the other is."""
    s = config_store.load_settings().source
    others = [n for n in NAMES if n != s.primary]
    usable = [n for n in (s.primary, *others) if ready(n)]
    if not usable:
        return [s.primary]  # nothing is set up: the first call explains what to do
    return usable if s.fallback else usable[:1]


def primary() -> str:
    return order()[0]


def browser(name: str):
    return _BROWSERS[name]


def primary_browser():
    return _BROWSERS[primary()]


def label(name: str) -> str:
    return NAMES.get(name, name)
