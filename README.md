# Lucid

A clean web UI for [streamrip](https://github.com/nathom/streamrip), built to feed a [Navidrome](https://www.navidrome.org/) library. Search a catalogue, browse artists and albums, queue downloads, and have your files tagged the way Navidrome actually reads them.

It ships two ways from one codebase:

- **A single container** (Podman or Docker) with streamrip and ffmpeg baked in, meant to run next to Navidrome on a home server.
- **A Windows desktop app** (Tauri) that bundles the whole backend and runs fully offline on one PC.

## Features

- **Search that works like a music app.** One search box returns albums, artists, songs and playlists together. Filter by type, then open an album, artist, song or playlist page. Paste a Qobuz link into the search bar and it opens that page directly.
- **Live queue.** Downloads show a progress bar, MB downloaded and MB/s. Pasted links show the real track or album name instead of the URL.
- **Navidrome-friendly tagging.** Streamrip joins multiple artists into one string (`"Alice, Bob"`), which Navidrome turns into a single phantom artist. Lucid re-tags Qobuz downloads from the structured API data instead, so artists, roles, compilations and multi-disc albums come out right. See [docs/navidrome-tagging.md](docs/navidrome-tagging.md).
- **Safe moves into your library.** Each job downloads into its own staging folder and is moved in only when finished (via a `.part` rename, so Navidrome never indexes half a file). A lower-quality file never overwrites a higher-quality one.
- **Navidrome rescan on completion** through the Subsonic API.
- **Sources:** Qobuz, Tidal, Deezer, SoundCloud, plus last.fm playlists. Browsing is Qobuz-only; the other sources work by pasting a link. YouTube (optional, via yt-dlp) also shows up in Search. Its audio is lossy and is saved as YouTube serves it (Opus or AAC), never converted to FLAC. If YouTube asks you to sign in, paste a cookies.txt under Settings > YouTube.
- **Spotify browsing (optional):** with a Spotify developer app (client ID and secret under Settings > Spotify; Spotify requires the app owner to have Premium), search, artist pages and album pages show Spotify's catalogue, photos and track lists, so Qobuz's wrong-artist uploads never appear. Qobuz is only used to find the files when you download or preview, and monitoring/library check use the same Spotify data. Nothing is downloaded from Spotify. Results show where a download would come from and at what quality (Qobuz lossless, or YouTube lossy). If Qobuz doesn't have a release, it's downloaded from YouTube track by track and tagged from Spotify's info; turn that off with the fallback option under Settings > YouTube. A Spotify switch next to the search box turns it off per search. Settings > Library check scans what you've already downloaded, lists albums Spotify doesn't list for their artist, and lets you delete selected tracks or all of them.
- **Desktop extras:** native folder picker for the download location.

## Quick start (container)

```sh
podman build -t lucid -f Containerfile .

podman run -d \
  --name lucid \
  -p 8787:8787 \
  -v lucid-music:/music \
  -v lucid-config:/config \
  lucid
```

Open `http://localhost:8787`. Replace `podman` with `docker` if you prefer; the same Containerfile builds with both.

- `/music` is where downloads land. Mount the same host folder Navidrome uses as its library.
- `/config` holds your settings, credentials and download history. Keep it on a volume.
- If Navidrome runs in another container, put both on one network (`podman network create lucid-net`, then `--network lucid-net` on each) so Lucid can reach it by name.

### Protect the UI

Lucid stores streaming credentials. If it's reachable beyond your own machine, add HTTP basic auth:

```sh
-e LUCID_USERNAME=someuser -e LUCID_PASSWORD='something-long'
```

## First-time setup

Open **Settings**.

**Qobuz.** Email and password login is broken on Qobuz's side for third-party apps (you get a 401 even with the right password), so use an auth token:

1. Log in at `play.qobuz.com`.
2. Open DevTools, go to the Network tab and reload.
3. Find the `user/login` request and open its response.
4. Copy `user.id` into **User ID** and `user_auth_token` into **Auth token**, and switch on "Use auth token".

It's a session credential, so if downloads start failing later, grab a fresh one the same way.

**Tidal** needs a one-time interactive login: `podman exec -it lucid rip config --tidal`.

**Deezer** needs your `arl` cookie, and **SoundCloud** a client ID and app version (see the hints in Settings).

**Navidrome.** Enter its URL and a user, press *Test connection*, then switch it on. Every finished download triggers a library scan. Set the **Navidrome version** under tagging to match your server; it controls which tags are safe to write.

## Windows desktop app

A standalone build with its own backend, streamrip and ffmpeg, no server needed. Build and run instructions are in [desktop/README.md](desktop/README.md). It stores config under `%APPDATA%\com.lucid.app` and downloads to `Music\Lucid` by default, changeable from Settings with a native folder picker. Point it at the same Navidrome server over the network and it behaves like the container.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LUCID_MUSIC_DIR` | `/music` | Download root |
| `LUCID_CONFIG_DIR` | `/config` | Settings, credentials, history |
| `LUCID_USERNAME` / `LUCID_PASSWORD` | unset | Enable basic auth on the UI |
| `LUCID_JOB_TIMEOUT_SECONDS` | `7200` | Kill a single stuck download after this long |
| `LUCID_RIP_BIN` | `rip` | Path to the streamrip CLI |
| `LUCID_FRONTEND_DIR` | `/app/frontend` | Where the UI files are served from |

Folder and file naming is editable in Settings. The default is `<AlbumArtist>/<Album> (<Year>)/<track>`, which nests cleanly for Navidrome.

## How it works

Lucid is a FastAPI backend and a plain HTML/CSS/JS frontend (no build step). Downloads shell out to the `rip` CLI, since streamrip has no supported Python API. Search and artist/album pages are served from Qobuz's catalogue API through streamrip's own authenticated client. After each download, the files are re-tagged from the raw API response (rather than streamrip's already-flattened strings), then moved into your library.

```
backend/    FastAPI app: browse, jobs, tagging, Navidrome client
frontend/   The UI (vanilla JS, no framework)
desktop/    Tauri shell and PyInstaller build for Windows
docs/       Navidrome tagging reference
Containerfile
```

## Development

```sh
pip install -r backend/requirements.txt
cd backend
LUCID_MUSIC_DIR=./music LUCID_CONFIG_DIR=./config LUCID_FRONTEND_DIR=../frontend \
  python -m uvicorn app.main:app --port 8787
```

Python 3.11+. The `rip` command is installed by the `streamrip` requirement.

## Known limitations

- Search and browsing is Qobuz only.
- Only FLAC files are re-tagged; MP3 and M4A are left as streamrip wrote them.
- Qobuz doesn't label releases as album, EP or single, so those labels are inferred from track count and length.
- The progress percentage is an estimate (Qobuz only); other sources show MB and speed without a percentage.
- On Windows, SoundCloud downloads can fail inside streamrip itself (a streamrip issue, not Lucid's).
- Tagging rules for Tidal and Deezer aren't implemented yet.

## Disclaimer

Lucid is a personal-use tool for downloading from accounts you pay for. You are responsible for following each service's terms and the copyright law where you live. It is not affiliated with Qobuz, Tidal, Deezer, SoundCloud, Navidrome or streamrip.

## Credits

Built on [streamrip](https://github.com/nathom/streamrip) by nathom and designed to work with [Navidrome](https://www.navidrome.org/). The UI uses the [Inter](https://rsms.me/inter/) typeface.
