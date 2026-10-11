# Navidrome-friendly tagging (Qobuz)

Streamrip's own metadata layer joins multi-artist and multi-genre data into a
single comma-joined string before tagging (`", ".join(a["name"] for a in artists)`
in `streamrip/metadata/album.py`). Navidrome only splits a *singular* tag on a
fixed separator list (`" / "`, `" feat. "`, `" feat "`, `" ft. "`, `" ft "`,
`"; "`) — a joined string like `"Alice, Bob"` becomes one phantom artist. This
feature bypasses streamrip's flattening by independently fetching the raw
Qobuz API response (still structured) and re-tagging files after download.

Status: **Qobuz only.** Tidal/Deezer/SoundCloud downloads are unaffected for
now and go through the original path unchanged.

## How it works

1. Streamrip downloads into a per-job staging directory (`rip --folder <staging>`)
   instead of directly into the library, so Navidrome's scanner never sees a
   half-tagged file.
2. The raw Qobuz metadata for that album/track is fetched independently
   (`QobuzClient.get_metadata`), bypassing streamrip's own flattening.
3. Files in staging are matched to that metadata by the `TRACKNUMBER`/`DISCNUMBER`
   tags streamrip's own tagger already wrote correctly (not by filename —
   robust against a customized `track_format`, and against streamrip skipping
   an already-downloaded track).
4. Tags are corrected in place with mutagen, then the whole staging directory
   is moved into the library, preserving the relative folder structure
   streamrip already built from `folder_format`/`track_format`. A destination
   that already exists (retry/re-download) is overwritten.

Toggle: Settings → "Navidrome-friendly tagging" (on by default). Per-download
override available next to the search form (use setting default / force on /
force off).

## Album-level tags (computed once, identical on every track)

| Tag | Source |
|---|---|
| `ALBUMARTISTS` (repeated) + `ALBUMARTIST` (joined " & ") | `artists[]` when more than one |
| `ALBUMARTIST` only | `artists[]` with one entry, or `artist.name` |
| `ALBUMARTIST=Various Artists` + `COMPILATION=1` on every track | album artist name exactly (case-insensitive) `"various artists"` — **name-based only**, Qobuz has no stable "Various Artists" ID (verified: searching turns up many distinct IDs for label-specific variants) |
| `GENRE` (repeated, never joined) | `genres_list`, split on `/` and `→`, deduped |
| `RELEASEDATE`/`YEAR` | `release_date_original` |
| `ORIGINALDATE` | only written when `release_date_original` actually differs from `release_date_download`/`_stream` — **never** inferred from a title keyword like "Remaster"/"Deluxe" (logged as a note instead) |
| `LABEL` | `label.name` |
| `BARCODE` | `upc` |
| `ALBUMCOMPOSER` | album-level `composer.name`, when present |
| `TRACKTOTAL` / `DISCTOTAL` | `tracks_count` / max `media_number` |

`DATE` (recording date) is deliberately left unset — Qobuz exposes no real
recording-date data — and any stale `DATE` streamrip itself wrote is actively
cleared so it doesn't linger with a misleading value.

## Track-level tags

| Tag | Source |
|---|---|
| `TRACKNUMBER` / `DISCNUMBER` | `track_number` / `media_number` |
| `ISRC` | `isrc` |
| `ARTIST` / `ARTISTS` + role tags | parsed from `performers` (see below) |

### `performers` parsing

Qobuz's `performers` field is usually clean machine-generated text:
`"Name, Role1, Role2 - Name2, Role"`. Some deep-catalog/reissue albums instead
carry hand-written liner-notes prose (nested parens, "Produced by X", "Role :
Name1, Name2" groupings) that breaks that assumption. Rather than guess, the
parser detects the telltale signs (unbalanced parens within a split fragment,
a colon inside what should be a name/role) and bails out for that track,
falling back to the track's separate always-clean `performer.name` field for
`ARTIST` and skipping role/credit extraction — logged, not silent.

| Qobuz role (space/case-normalized) | Navidrome tag |
|---|---|
| MainArtist | `ARTIST`/`ARTISTS` |
| FeaturedArtist | `ARTIST`/`ARTISTS`, after main artists |
| Producer | `PRODUCER` |
| Composer | `COMPOSER` |
| Lyricist | `LYRICIST` |
| ComposerLyricist | both `COMPOSER` and `LYRICIST` |
| Writer | `COMPOSER`, **only if** no `ComposerLyricist` entry exists on that track (ambiguous role, used only as a fallback); every fallback is logged |
| Arranger / Conductor / Remixer / DjMixer / Director | native matching tag |
| anything containing "mix" (Mixer, MixingEngineer, "Mixing Engineer") | `MIXER` |
| any other "...engineer" (Engineer, RecordingEngineer, MasteringEngineer, RecordingSecondEngineer) | `ENGINEER` — Navidrome has no finer-grained engineer tag, so mastering/recording/plain collapse into one bucket |
| anything else (instruments, Vocals, Programmer, Masterer, StudioMusician, ...) | `PERFORMER` with the literal text as sub-role — requires Navidrome 0.55.0+ |
| AssociatedPerformer, MusicPublisher | dropped — not a credit Navidrome models; logged |

## Navidrome version gating

Settings → "Navidrome version" controls which of the above actually gets
written, since older Navidrome releases can't read some of it:

| Tier | What's written |
|---|---|
| Below 0.45.0 | Only basic single-value tags (`ARTIST`, `ALBUM`, dates, etc.) — no multi-valued tags, role tags, or `ALBUMARTISTS`/`ARTISTS`/`GENRE` as multiple values at all |
| 0.45.0 – 0.50.x | Multi-valued tags work, but role tags and `PERFORMER` don't — dropped and logged |
| 0.51.1 – 0.54.x | Role tags (`COMPOSER`, `PRODUCER`, `MIXER`, `ENGINEER`, etc.) work; `PERFORMER` sub-roles don't — dropped and logged |
| 0.55.0+ (default) | Everything above |

## Known gaps (this phase)

- **Only FLAC gets retagged.** MP3 and MP4/M4A files are left exactly as
  streamrip wrote them, with a logged note — not a half-attempt. (MP3/ID3v2.4
  has one frame per concept, e.g. `TPE1`, so the Vorbis trick of a separate
  joined "display" tag alongside a distinct multi-value list isn't directly
  possible there, and MP4's repeated-atom behavior hasn't been verified
  against Navidrome.)
- Tidal/Deezer/SoundCloud: not yet implemented (next phase). Qobuz has by far
  the richest structured data of the four in streamrip's own client layer.

## Move safety

Each job downloads into its own staging directory under
`/config/streamrip/staging/<job_id>` — never inside `/music`, so Navidrome's
scanner can't see a half-tagged file. After retagging, files move into the
library via a `.part`-suffixed temp name in the destination directory,
followed by an atomic rename — this matters if staging and the library ever
end up on different filesystems, where a plain move falls back to copy+delete
and a scanner could index a half-written file under its real name mid-copy.

If a destination file already exists:
- **Audio files** are only overwritten if the new one isn't lower quality
  (lossless always beats lossy; otherwise compares bit depth × sample rate,
  or bitrate for lossy-vs-lossy). A would-be downgrade is skipped — the
  existing library file is left alone, and the staged file is kept at
  `/config/streamrip/leftover/<job_id>/...` for manual inspection rather than
  being discarded.
- **Non-audio files** (cover art, booklets) are always overwritten.
- Every overwrite and every skipped-downgrade is logged with both files'
  format/bit-depth/bitrate.

## Job log

Every tagging job ends with one `SUMMARY:` line covering files retagged,
files skipped (and why), how many `Writer`→`COMPOSER` fallbacks fired, how
many tracks had an unparseable `performers` field, and how many role credits
were dropped — plus a separate `[move] SUMMARY:` line for the move step.
