import os
import sys
from pathlib import Path

MUSIC_DIR = Path(os.environ.get("LUCID_MUSIC_DIR", "/music"))
CONFIG_DIR = Path(os.environ.get("LUCID_CONFIG_DIR", "/config"))

STREAMRIP_DIR = CONFIG_DIR / "streamrip"
STREAMRIP_CONFIG_PATH = STREAMRIP_DIR / "config.toml"
DOWNLOADS_DB_PATH = STREAMRIP_DIR / "downloads.db"
FAILED_DOWNLOADS_DB_PATH = STREAMRIP_DIR / "failed_downloads.db"

APP_SETTINGS_PATH = CONFIG_DIR / "settings.json"
JOBS_HISTORY_PATH = CONFIG_DIR / "jobs.json"
MONITORS_PATH = CONFIG_DIR / "monitors.json"
COVERS_DIR = CONFIG_DIR / "covers"

# Unless LUCID_RIP_BIN points at a specific streamrip CLI, run `rip` through our wrapper, which
# adds Tidal hi-res FLAC support and otherwise behaves exactly like `rip`.
RIP_COMMAND = (
    [os.environ["LUCID_RIP_BIN"]]
    if os.environ.get("LUCID_RIP_BIN")
    else [sys.executable, str(Path(__file__).with_name("rip_wrapper.py"))]
)
RIP_BIN = RIP_COMMAND[0]


def ensure_dirs() -> None:
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    STREAMRIP_DIR.mkdir(parents=True, exist_ok=True)


def resolve_music_subfolder(subfolder: str) -> Path:
    """Resolve a user-supplied relative path under MUSIC_DIR, rejecting anything
    (via "..", absolute paths, symlinks) that would land outside the mount."""
    sub = (subfolder or "").strip().strip("/\\")
    root = MUSIC_DIR.resolve()
    if not sub:
        return root
    candidate = (MUSIC_DIR / sub).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("Download folder must stay inside the /music mount")
    return candidate
