import hashlib
import secrets

import httpx

from .schemas import NavidromeSettings

CLIENT_NAME = "lucid"
API_VERSION = "1.16.1"


def _auth_params(username: str, password: str) -> dict:
    salt = secrets.token_hex(6)
    token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()
    return {"u": username, "t": token, "s": salt, "v": API_VERSION, "c": CLIENT_NAME, "f": "json"}


async def _get(nav: NavidromeSettings, endpoint: str) -> dict:
    url = nav.url.rstrip("/") + f"/rest/{endpoint}"
    params = _auth_params(nav.username, nav.password)
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


async def test_connection(nav: NavidromeSettings) -> tuple[bool, str]:
    if not nav.url or not nav.username:
        return False, "Navidrome URL and username are required"
    try:
        data = await _get(nav, "ping")
        status = data.get("subsonic-response", {}).get("status")
        if status == "ok":
            return True, "Connected"
        err = data.get("subsonic-response", {}).get("error", {}).get("message", "Unknown error")
        return False, err
    except httpx.HTTPError as exc:
        return False, str(exc)


async def trigger_scan(nav: NavidromeSettings) -> tuple[bool, str]:
    if not nav.enabled or not nav.url or not nav.username:
        return False, "Navidrome integration not configured"
    try:
        data = await _get(nav, "startScan")
        status = data.get("subsonic-response", {}).get("status")
        if status == "ok":
            return True, "Scan triggered"
        err = data.get("subsonic-response", {}).get("error", {}).get("message", "Unknown error")
        return False, err
    except httpx.HTTPError as exc:
        return False, str(exc)
