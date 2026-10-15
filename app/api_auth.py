"""The local API key every mybot client presents to the chat server.

The server creates it on first start (~/.mybot/api-key, mode 0600). The
bridges, the menu app and the owner's own tools read it; answer agents never
can (their sandbox denies reads under ~), they get a per-request grant
instead — see server.py's AGENT_GRANT_PATHS.
"""
from __future__ import annotations

import os
import secrets
import urllib.request
from pathlib import Path

API_KEY_PATH = Path(os.environ.get("MYBOT_API_KEY_PATH", "~/.mybot/api-key")).expanduser()


def load_or_create_api_key(path: Path = API_KEY_PATH) -> str:
    key = read_api_key(path)
    if key:
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    key = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(key + "\n")
    return key


def read_api_key(path: Path = API_KEY_PATH) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def authorize(request: urllib.request.Request) -> urllib.request.Request:
    """Attach the API key to a request bound for the chat server."""
    key = read_api_key()
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    return request
