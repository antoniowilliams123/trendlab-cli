"""Secret redaction.

Applied to everything that leaves the process boundary in a non-execution
role: notifications, the remote approval payload, the audit log. The agent
still executes the real command; only what is *shown/stored* is redacted.
"""

from __future__ import annotations

import os
import re
from typing import Any

REDACTED = "[REDACTED]"

# A *name* looks secret when it ends in one of these words (api_key, GITHUB_TOKEN, db_password).
# Plural/compound counters such as input_tokens, est_tokens or token_budget are not secrets.
_SECRET_NAME_HINT = re.compile(
    r"(?:^|[_\-.])(api[_\-]?key|key|token|secret|password|passwd|credential|authorization)$", re.I
)

_PATTERNS: list[re.Pattern[str]] = [
    # key=value / key: value style assignments with a secret-looking key.
    re.compile(
        r"(?i)\b([A-Z0-9_\-]*(?:api[_\-]?key|token|secret|password|passwd|credential|"
        r"authorization)[A-Z0-9_\-]*)(\s*[=:]\s*)(['\"]?)([^\s'\"&;]+)\3"
    ),
    # Authorization bearer tokens.
    re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9\-._~+/]+=*)"),
    # Well-known key shapes.
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{12,})"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})"),
    re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9\-]{10,})"),
    # Telegram bot tokens: digits:alnum.
    re.compile(r"\b(\d{8,10}:[A-Za-z0-9_\-]{30,})"),
    # Private key blocks.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]


def _env_secret_values() -> list[str]:
    values = []
    for name, value in os.environ.items():
        if _SECRET_NAME_HINT.search(name) and len(value) >= 8:
            values.append(value)
    # Longest first so partial overlaps redact fully.
    return sorted(set(values), key=len, reverse=True)


def redact_text(text: str) -> str:
    if not text:
        return text
    out = text
    for value in _env_secret_values():
        out = out.replace(value, REDACTED)
    # Bearer tokens first, so "Authorization: Bearer <tok>" does not leave <tok> behind.
    out = _PATTERNS[1].sub(lambda m: f"{m.group(1)} {REDACTED}", out)
    out = _PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    for pat in _PATTERNS[2:]:
        out = pat.sub(REDACTED, out)
    return out


def redact_structure(value: Any) -> Any:
    """Recursively redact strings inside dicts/lists; drop keys that look like secrets."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if (
                isinstance(k, str)
                and _SECRET_NAME_HINT.search(k)
                and k.lower()
                not in {
                    "decision_token_present",
                    "token_file",
                }
            ):
                out[k] = REDACTED
            else:
                out[k] = redact_structure(v)
        return out
    if isinstance(value, list | tuple):
        return [redact_structure(v) for v in value]
    return value
