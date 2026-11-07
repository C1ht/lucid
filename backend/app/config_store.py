import hashlib
import importlib.resources
import json
import threading
from pathlib import Path

import tomlkit

from . import paths, youtube
from .schemas import SECRET_UNCHANGED, Settings
from .streamrip_template import DEFAULT_CONFIG_TOML

_lock = threading.Lock()

# Fields that hold secrets: we never echo these back to the client, and an
# incoming empty string means "leave the stored value alone" rather than "clear it".
_SECRET_FIELDS = {
    ("qobuz", "password_or_token"),
    ("tidal", "access_token"),
    ("tidal", "refresh_token"),
    ("deezer", "arl"),
    ("soundcloud", "client_id"),
    ("youtube", "cookies"),
    ("navidrome", "password"),
}


def load_settings() -> Settings:
    if not paths.APP_SETTINGS_PATH.exists():
        return Settings()
    with paths.APP_SETTINGS_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return Settings.model_validate(raw)


def redact(settings: Settings) -> dict:
    """Settings dict safe to send to the browser: secrets replaced with a presence flag."""
    data = settings.model_dump()
    for section, field in _SECRET_FIELDS:
        value = data[section][field]
        data[section][field] = ""
        data[section][f"{field}_set"] = bool(value)
    return data


def save_settings(incoming: Settings) -> Settings:
    with _lock:
        current = load_settings()
        merged = incoming.model_dump()
        current_dump = current.model_dump()
        for section, field in _SECRET_FIELDS:
            if merged[section][field] == SECRET_UNCHANGED:
                merged[section][field] = current_dump[section][field]
        settings = Settings.model_validate(merged)
        paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with paths.APP_SETTINGS_PATH.open("w", encoding="utf-8") as f:
            json.dump(settings.model_dump(), f, indent=2)
        write_streamrip_config(settings)
        _write_youtube_cookies(settings.youtube.cookies)
        return settings


def _write_youtube_cookies(cookies: str) -> None:
    path = youtube.COOKIES_PATH
    if not cookies.strip():
        path.unlink(missing_ok=True)
        return
    path.write_text(cookies.strip() + "\n", encoding="utf-8")
    path.chmod(0o600)


def _packaged_default_doc():
    """streamrip's own default config for the *installed* version. Building from
    this (rather than a hand-copied template) keeps our file in lockstep with
    whatever schema that version actually accepts."""
    try:
        text = importlib.resources.files("streamrip").joinpath("config.toml").read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 - fall back to the embedded copy
        text = DEFAULT_CONFIG_TOML
    return tomlkit.parse(text)


def _conform_to_streamrip(doc) -> None:
    """Make `doc` loadable by the installed streamrip: correct version string,
    no keys it doesn't know (it rejects them), none missing. Existing values
    (credentials, paths, app_id/secrets) are always kept."""
    from streamrip.config import CURRENT_CONFIG_VERSION

    default = _packaged_default_doc()
    for section, table in default.items():
        if section not in doc:
            doc[section] = {k: v for k, v in table.items()}
            continue
        for key in list(doc[section].keys()):
            if key not in table:
                del doc[section][key]
        for key, value in table.items():
            if key not in doc[section]:
                doc[section][key] = value
    for section in list(doc.keys()):
        if section not in default:
            del doc[section]
    doc["misc"]["version"] = CURRENT_CONFIG_VERSION


def repair_streamrip_config() -> None:
    """Fix an existing config.toml written against a different schema version."""
    path = paths.STREAMRIP_CONFIG_PATH
    if not path.exists():
        return
    with _lock:
        before = path.read_text(encoding="utf-8")
        doc = tomlkit.parse(before)
        _conform_to_streamrip(doc)
        after = tomlkit.dumps(doc)
        if after != before:
            path.write_text(after, encoding="utf-8")


def resolve_download_folder(settings: Settings) -> Path:
    if settings.downloads.absolute_path:
        folder = Path(settings.downloads.absolute_path)
        if not folder.is_absolute():
            raise ValueError("Download folder must be an absolute path")
        return folder
    return paths.resolve_music_subfolder(settings.downloads.subfolder)


def write_streamrip_config(settings: Settings) -> None:
    paths.ensure_dirs()
    if paths.STREAMRIP_CONFIG_PATH.exists():
        doc = tomlkit.parse(paths.STREAMRIP_CONFIG_PATH.read_text(encoding="utf-8"))
    else:
        doc = _packaged_default_doc()
    _conform_to_streamrip(doc)

    download_folder = resolve_download_folder(settings)
    download_folder.mkdir(parents=True, exist_ok=True)
    doc["downloads"]["folder"] = str(download_folder)

    q = settings.qobuz
    doc["qobuz"]["use_auth_token"] = q.use_auth_token
    doc["qobuz"]["email_or_userid"] = q.email_or_userid
    # Qobuz's own API wants the MD5 hash of the password here, not the
    # plaintext (the auth-token mode is the one case where this field really
    # is a token, passed through as-is). We hash server-side so the Settings
    # form can just ask for "your password" like any normal login.
    if q.use_auth_token or not q.password_or_token:
        doc["qobuz"]["password_or_token"] = q.password_or_token
    else:
        doc["qobuz"]["password_or_token"] = hashlib.md5(q.password_or_token.encode("utf-8")).hexdigest()
    doc["qobuz"]["quality"] = q.quality

    # Tidal tokens are obtained out-of-band via `rip config --tidal`, which writes
    # them straight into config.toml. Only touch a field here if we actually have
    # a value for it, so saving settings never clobbers a working login.
    t = settings.tidal
    for key, value in (
        ("user_id", t.user_id),
        ("country_code", t.country_code),
        ("access_token", t.access_token),
        ("refresh_token", t.refresh_token),
        ("token_expiry", t.token_expiry),
    ):
        if value:
            doc["tidal"][key] = value
    doc["tidal"]["quality"] = t.quality

    d = settings.deezer
    doc["deezer"]["arl"] = d.arl
    doc["deezer"]["quality"] = d.quality

    s = settings.soundcloud
    doc["soundcloud"]["client_id"] = s.client_id
    doc["soundcloud"]["app_version"] = s.app_version

    y = settings.youtube
    doc["youtube"]["download_videos"] = y.download_videos

    c = settings.conversion
    doc["conversion"]["enabled"] = c.enabled
    doc["conversion"]["codec"] = c.codec
    doc["conversion"]["sampling_rate"] = c.sampling_rate
    doc["conversion"]["bit_depth"] = c.bit_depth
    doc["conversion"]["lossy_bitrate"] = c.lossy_bitrate

    fp = settings.filepaths
    doc["filepaths"]["folder_format"] = fp.folder_format
    doc["filepaths"]["track_format"] = fp.track_format
    doc["filepaths"]["restrict_characters"] = fp.restrict_characters

    # Plain log lines instead of animated progress bars: our job log and
    # progress tracking read rip's output / the files it writes, not a terminal.
    doc["cli"]["progress_bars"] = False

    doc["database"]["downloads_path"] = str(paths.DOWNLOADS_DB_PATH)
    doc["database"]["failed_downloads_path"] = str(paths.FAILED_DOWNLOADS_DB_PATH)

    paths.STREAMRIP_CONFIG_PATH.write_text(tomlkit.dumps(doc), encoding="utf-8")


def ensure_streamrip_config() -> None:
    """Make sure a config.toml exists on first boot, seeded from current settings."""
    paths.ensure_dirs()
    if not paths.STREAMRIP_CONFIG_PATH.exists():
        write_streamrip_config(load_settings())
    else:
        repair_streamrip_config()
