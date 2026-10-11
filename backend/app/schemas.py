from typing import Literal, Optional

from pydantic import BaseModel, Field

SECRET_UNCHANGED = ""  # empty string from the client means "leave stored value as-is"


class QobuzSettings(BaseModel):
    enabled: bool = False
    use_auth_token: bool = False
    email_or_userid: str = ""
    password_or_token: str = ""
    quality: int = 3


class TidalSettings(BaseModel):
    enabled: bool = False
    # Populated by `rip config --tidal` run once inside the container; see README.
    user_id: str = ""
    country_code: str = ""
    access_token: str = ""
    refresh_token: str = ""
    token_expiry: str = ""
    quality: int = 3


class DeezerSettings(BaseModel):
    enabled: bool = False
    arl: str = ""
    quality: int = 2


class SoundcloudSettings(BaseModel):
    enabled: bool = False
    client_id: str = ""
    app_version: str = ""


class YoutubeSettings(BaseModel):
    enabled: bool = False
    download_videos: bool = False


class ConversionSettings(BaseModel):
    enabled: bool = False
    codec: Literal["FLAC", "ALAC", "OPUS", "MP3", "VORBIS", "AAC"] = "ALAC"
    sampling_rate: int = 48000
    bit_depth: int = 24
    lossy_bitrate: int = 320


class FilepathSettings(BaseModel):
    folder_format: str = "{albumartist}/{title} ({year})"
    track_format: str = "{tracknumber:02}. {artist} - {title}{explicit}"
    restrict_characters: bool = False


class DownloadsSettings(BaseModel):
    # Relative path under the /music mount. Empty means the mount root itself.
    subfolder: str = ""
    # Desktop app only: an absolute path picked via the native folder dialog.
    # When set, this overrides `subfolder` entirely and isn't sandboxed to
    # the music mount -- there is no mount to sandbox to on desktop, and the
    # path came from an explicit native dialog choice, not free-text input.
    absolute_path: str = ""


NavidromeVersionTier = Literal["pre_0_45", "0_45_to_0_50", "0_51_to_0_54", "0_55_plus"]


class TaggingSettings(BaseModel):
    enabled: bool = True
    navidrome_version_tier: NavidromeVersionTier = "0_55_plus"


class NavidromeSettings(BaseModel):
    enabled: bool = False
    url: str = ""
    username: str = ""
    password: str = ""


class Settings(BaseModel):
    qobuz: QobuzSettings = Field(default_factory=QobuzSettings)
    tidal: TidalSettings = Field(default_factory=TidalSettings)
    deezer: DeezerSettings = Field(default_factory=DeezerSettings)
    soundcloud: SoundcloudSettings = Field(default_factory=SoundcloudSettings)
    youtube: YoutubeSettings = Field(default_factory=YoutubeSettings)
    conversion: ConversionSettings = Field(default_factory=ConversionSettings)
    filepaths: FilepathSettings = Field(default_factory=FilepathSettings)
    downloads: DownloadsSettings = Field(default_factory=DownloadsSettings)
    tagging: TaggingSettings = Field(default_factory=TaggingSettings)
    navidrome: NavidromeSettings = Field(default_factory=NavidromeSettings)


Source = Literal["qobuz", "tidal", "deezer", "soundcloud"]
MediaType = Literal["track", "album", "artist", "playlist", "label"]


class SearchRequest(BaseModel):
    source: Source
    media_type: MediaType
    query: str


class SearchResult(BaseModel):
    source: str
    media_type: str
    id: str
    desc: str


class DownloadByIdRequest(BaseModel):
    source: Source
    media_type: MediaType
    id: str
    desc: Optional[str] = None
    navidrome_tagging: Optional[bool] = None


class DownloadByUrlRequest(BaseModel):
    url: str
    navidrome_tagging: Optional[bool] = None


JobKind = Literal["id", "url", "lastfm"]
JobStatus = Literal["queued", "running", "success", "failed", "canceled"]


class JobOut(BaseModel):
    id: str
    kind: JobKind
    label: str
    status: JobStatus
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    log: list[str] = Field(default_factory=list)
    error: Optional[str] = None
    bytes_downloaded: int = 0
    speed_mbps: float = 0.0
    # Approximate -- see progress.py. None when we have no size estimate to
    # compare against (e.g. non-Qobuz sources), not "0% done".
    percent: Optional[float] = None
