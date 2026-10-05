"""Remote access token: one long-lived bearer secret per TrendLab install.

Stored at ``$TRENDLAB_HOME/remote_token`` with mode 0600. The phone is paired
by opening a URL that carries the token in the *fragment* (``#t=...``), which
browsers never send to the server or write to access logs. Rotating the token
invalidates every paired device.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from trendlab.config.loader import trendlab_home

TOKEN_FILE = "remote_token"


def token_path() -> Path:
    return trendlab_home() / TOKEN_FILE


def load_or_create_token() -> str:
    path = token_path()
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    return rotate_token()


def rotate_token() -> str:
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


def pairing_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/#t={token}"
