"""Fetches raw metadata from a source's own (unsupported, but functionally
stable) streamrip client class -- there's no public API, and this reuses the
exact auth/session the user already configured. All four source clients
share the same `get_metadata(item_id, media_type) -> dict` shape."""

from streamrip.client.deezer import DeezerClient
from streamrip.client.qobuz import QobuzClient
from streamrip.client.soundcloud import SoundcloudClient
from streamrip.client.tidal import TidalClient
from streamrip.config import Config

from . import paths

_CLIENT_CLASSES = {
    "qobuz": QobuzClient,
    "tidal": TidalClient,
    "deezer": DeezerClient,
    "soundcloud": SoundcloudClient,
}


async def fetch_metadata(source: str, item_id: str, media_type: str) -> dict:
    client_cls = _CLIENT_CLASSES[source]
    config = Config(str(paths.STREAMRIP_CONFIG_PATH))
    client = client_cls(config)
    await client.login()
    try:
        return await client.get_metadata(item_id, media_type)
    finally:
        session = getattr(client, "session", None)
        if session is not None:
            await session.close()
