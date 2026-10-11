# Default config.toml shipped with streamrip (schema version 2.2.0), used as the
# base document we patch with tomlkit so comments/structure stay intact and any
# field we don't manage keeps streamrip's own default.
DEFAULT_CONFIG_TOML = '''\
[downloads]
folder = ""
source_subdirectories = false
disc_subdirectories = true
concurrency = true
max_connections = 6
requests_per_minute = 60
verify_ssl = true

[qobuz]
quality = 3
download_booklets = true
use_auth_token = false
email_or_userid = ""
password_or_token = ""
app_id = ""
secrets = []

[tidal]
quality = 3
download_videos = true
user_id = ""
country_code = ""
access_token = ""
refresh_token = ""
token_expiry = ""

[deezer]
quality = 2
lower_quality_if_not_available = true
arl = ""
use_deezloader = true
deezloader_warnings = true

[soundcloud]
quality = 0
client_id = ""
app_version = ""

[youtube]
quality = 0
download_videos = false
video_downloads_folder = ""

[database]
downloads_enabled = true
downloads_path = ""
failed_downloads_enabled = true
failed_downloads_path = ""

[conversion]
enabled = false
codec = "ALAC"
sampling_rate = 48000
bit_depth = 24
lossy_bitrate = 320

[qobuz_filters]
extras = false
repeats = false
non_albums = false
features = false
non_studio_albums = false
non_remaster = false

[artwork]
embed = true
embed_size = "large"
embed_max_width = -1
save_artwork = true
saved_max_width = -1

[metadata]
set_playlist_to_album = true
renumber_playlist_tracks = true
exclude = []

[filepaths]
add_singles_to_folder = false
folder_format = "{albumartist} - {title} ({year}) [{container}] [{bit_depth}B-{sampling_rate}kHz]"
track_format = "{tracknumber:02}. {artist} - {title}{explicit}"
restrict_characters = false
truncate_to = 120

[lastfm]
source = "qobuz"
fallback_source = ""

[cli]
text_output = true
progress_bars = false
max_search_results = 200

[misc]
version = "2.2.0"
check_for_updates = true
'''
