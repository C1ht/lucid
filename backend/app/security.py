import base64
import os
import secrets
from typing import Optional

from fastapi import HTTPException, Request

UNAUTHORIZED = HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic realm=\"lucid\""})


def _expected_credentials() -> Optional[tuple[str, str]]:
    username = os.environ.get("LUCID_USERNAME")
    password = os.environ.get("LUCID_PASSWORD")
    if username and password:
        return username, password
    return None


def check_header(header_value: Optional[str]) -> bool:
    expected = _expected_credentials()
    if expected is None:
        return True
    if not header_value or not header_value.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header_value[6:]).decode("utf-8")
        username, _, password = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return secrets.compare_digest(username, expected[0]) and secrets.compare_digest(password, expected[1])


def require_auth(request: Request) -> None:
    if not check_header(request.headers.get("authorization")):
        raise UNAUTHORIZED
