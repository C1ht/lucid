# Lucid Desktop

A standalone Windows build of Lucid: a Tauri shell that bundles the whole
backend (Python, streamrip, ffmpeg) and runs fully offline, independent of
the container/Navidrome-homeserver setup. Point it at the same Navidrome
server over the network in Settings and it works the same way.

## Architecture

Tauri's Rust shell doesn't render its own UI — on startup it:

1. Spawns `lucid-backend` (the FastAPI backend, frozen to a single `.exe`
   with PyInstaller) as a sidecar process, with `rip.exe` (streamrip's CLI,
   also frozen) and `ffmpeg.exe` on its `PATH`.
2. Polls `http://127.0.0.1:8799/api/health` until it responds.
3. Navigates the native window to `http://127.0.0.1:8799/` — the backend
   serves the exact same frontend as the container does. No separate desktop
   UI to maintain.
4. Kills the sidecar when the window closes.

Config and downloaded music live under the OS's normal app-data locations
(`%APPDATA%\com.lucid.app` for config, `%USERPROFILE%\Music\Lucid` for
downloads by default) — set via env vars the Rust shell passes to the
sidecar, the same `LUCID_CONFIG_DIR`/`LUCID_MUSIC_DIR` the container uses.

Settings → "Downloads location & naming" gets an extra "Download folder" row
on desktop (hidden in the container/browser UI, where there's no native
dialog to call): a **Browse…** button opens the real native Windows folder
picker (`tauri-plugin-dialog`), and the chosen absolute path overrides the
subfolder setting entirely — unlike that one, it isn't sandboxed to any
mount point, since it's an explicit dialog choice rather than free-text
input. Clear it to fall back to the default `Music\Lucid` folder.

## Building

Prerequisites: Rust (`rustup`), Node, Python 3.11+, and a real `ffmpeg.exe`
somewhere on disk.

```sh
# 1. Build the frozen backend + rip executables, place them for Tauri to bundle
./build-backend.ps1 -FfmpegPath "C:\path\to\ffmpeg.exe"

# 2. Dev run (fast iteration)
npm install
npm run tauri dev

# 3. Release build (installer under src-tauri/target/release/bundle/)
npm run tauri build
```

Re-run `build-backend.ps1` after any change to `backend/app/**` — Tauri
doesn't know to rebuild the Python side on its own.

## Known gaps / follow-ups

- **ffmpeg size**: bundled from whatever full build is on hand during
  development (~200MB — gyan.dev's "full" build includes far more codecs
  than needed). Swap in an "essentials" static build before shipping to
  shrink this a lot.
- **Orphaned sidecar on crash**: if the app is force-killed (crash, Task
  Manager "End task" rather than closing the window) rather than exiting
  normally, the backend sidecar can be left running in the background
  holding port 8799, since Windows doesn't tie child process lifetime to
  the parent by default. A clean window close goes through
  `on_window_event(Destroyed)` and kills it correctly; this edge case
  doesn't yet. Wrapping the spawn in a Windows Job Object would fix it —
  not done yet.
- **SoundCloud downloads can fail on Windows** with `[Errno 2] No such file
  or directory` on the final muxed file — reproduced identically against a
  plain (non-frozen) streamrip install too, so it's a pre-existing
  streamrip-on-Windows issue with SoundCloud's ffmpeg-muxed HLS downloads,
  not something this packaging introduced. Qobuz (the primary use case, no
  muxing needed) downloads correctly.
- Single instance isn't enforced — launching the app twice would try to bind
  port 8799 twice and the second instance's backend would fail to start.
