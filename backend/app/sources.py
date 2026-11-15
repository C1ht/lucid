"""Which service the files are downloaded from: Qobuz or Tidal.

The reference catalogue (MusicBrainz and friends) decides what exists; this module knows the
services the files can come from, which one is preferred, and which are usable right now.
"""

from . import config_store
from .browse import browser as qobuz_browser
from .tidal_browse import browser as tidal_browser

NAMES = {"qobuz": "Qobuz", "tidal": "Tidal"}
_BROWSERS = {"qobuz": qobuz_browser, "tidal": tidal_browser}


def ready(name: str) -> bool:
    s = config_store.load_settings()
    if name == "tidal":
        return config_store.tidal_ready()
    q = s.qobuz
    return bool(q.enabled and q.email_or_userid and q.password_or_token)


def order() -> list[str]:
    """Sources to try, best first. The preferred one leads, the other follows when the fallback is
    on and it is set up. A preferred source that isn't set up is skipped if the other is."""
    s = config_store.load_settings().source
    other = "tidal" if s.primary == "qobuz" else "qobuz"
    usable = [n for n in (s.primary, other) if ready(n)]
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
