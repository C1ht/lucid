"""Logging in to Tidal from Settings, so no `podman exec -it ... rip config --tidal` is needed.

Tidal uses a device login: we get a link, the user opens it and approves in their own browser
(signed in to their own Tidal account), and we poll until Tidal hands over the tokens. They are
stored in streamrip's config, which is what downloads and browsing both read.
"""

import asyncio
from typing import Optional

from streamrip.client.tidal import TidalClient
from streamrip.config import Config

from . import config_store, paths

POLL_SECONDS = 3
GIVE_UP_AFTER = 5 * 60


class State:
    status = "idle"  # idle | waiting | done | error
    url: Optional[str] = None
    error: Optional[str] = None
    task: Optional[asyncio.Task] = None

    def public(self) -> dict:
        return {"status": self.status, "url": self.url, "error": self.error, "logged_in": config_store.tidal_ready()}


state = State()


async def _run() -> None:
    client = None
    try:
        config_store.ensure_streamrip_config()
        client = TidalClient(Config(str(paths.STREAMRIP_CONFIG_PATH)))
        device_code, url = await client._get_device_code()
        state.url = url if url.startswith("http") else f"https://{url}"
        waited = 0
        while waited < GIVE_UP_AFTER:
            await asyncio.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
            code, info = await client._get_auth_status(device_code)
            if code == 0:
                config_store.store_tidal_login(
                    {
                        "user_id": str(info["user_id"]),
                        "country_code": str(info["country_code"]),
                        "access_token": str(info["access_token"]),
                        "refresh_token": str(info["refresh_token"]),
                        "token_expiry": str(info["token_expiry"]),
                    }
                )
                state.status = "done"
                return
            if code == 1:
                raise RuntimeError("Tidal refused the login. Start again.")
        raise RuntimeError("The login link expired. Start again.")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        text = str(exc) or type(exc).__name__
        if "403" in text and "mimetype" in text:
            # Tidal's login server answered with a block page instead of JSON.
            text = (
                "Tidal's login server refused the request (403, an HTML block page instead of a login "
                "response). It happens with streamrip's built-in Tidal app credentials, not because of "
                "your account or network. Try again later, or use another source for now."
            )
        state.status, state.error = "error", text
    finally:
        session = getattr(client, "session", None)
        if session is not None:
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass


def start() -> dict:
    if state.task and not state.task.done():
        state.task.cancel()
    state.status, state.url, state.error = "waiting", None, None
    state.task = asyncio.create_task(_run())
    return state.public()
