"""Secret resolution: environment variable first, then ``~/.trendlab/secrets/<NAME>`` (mode 0600).

The secrets directory lets a user store a key once (``trendlab secret set NAME``) without
putting it in any file they might share or commit. Values are never logged or echoed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from trendlab.config.loader import trendlab_home

_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def secrets_dir() -> Path:
    return trendlab_home() / "secrets"


def _validate(name: str) -> str:
    if not _NAME.match(name):
        raise ValueError("secret names must look like OPENROUTER_API_KEY (upper-case, A-Z0-9_)")
    return name


def resolve_secret(name: str | None) -> str | None:
    """Return the secret value for ``name`` from the environment or the secrets store."""
    if not name:
        return None
    value = os.environ.get(name)
    if value:
        return value
    path = secrets_dir() / name
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def store_secret(name: str, value: str) -> Path:
    _validate(name)
    value = value.strip()
    if not value:
        raise ValueError("empty secret")
    d = secrets_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    path = d / name
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def delete_secret(name: str) -> bool:
    _validate(name)
    path = secrets_dir() / name
    if path.exists():
        path.unlink()
        return True
    return False


def list_secrets() -> list[str]:
    d = secrets_dir()
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_file() and _NAME.match(p.name))


def secret_source(name: str) -> str:
    """Where a secret would come from: 'env', 'store' or 'missing' (never the value)."""
    if os.environ.get(name):
        return "env"
    if (secrets_dir() / name).is_file():
        return "store"
    return "missing"
