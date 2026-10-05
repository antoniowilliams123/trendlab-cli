"""Secret scanning for content the agent is about to write (spec §36 'accidental disclosure').

Catches the obvious shapes before they land in the repository: provider keys, GitHub/AWS/Slack
tokens, Telegram bot tokens, private-key blocks, and ``SOMETHING_KEY = "<long value>"`` style
assignments. Placeholders (``...``, ``<your-key>``, ``${VAR}``, ``os.environ``) are ignored.
"""

from __future__ import annotations

import re

_SHAPES: list[tuple[str, re.Pattern[str]]] = [
    ("OpenAI/Anthropic-style key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{20,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "hard-coded secret assignment",
        re.compile(
            r"(?i)\b[A-Z0-9_]*(?:api[_\-]?key|secret|token|password|passwd)[A-Z0-9_]*\s*[:=]\s*"
            r"['\"]([A-Za-z0-9_\-/+=.]{16,})['\"]"
        ),
    ),
]
_PLACEHOLDER = re.compile(
    r"(\.\.\.|<[^>]+>|\$\{?[A-Z_]+\}?|os\.environ|getenv|xxx+|your[-_ ]?(api[-_ ]?)?key"
    r"|changeme|example)",
    re.I,
)
_DIFF_HEADER = re.compile(r"^(\+\+\+|---) (a/|b/|/dev/null)")


def find_secrets(text: str) -> list[str]:
    """Describe secret-looking values in ``text`` by line and kind (never the values)."""
    findings: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _DIFF_HEADER.match(line):
            continue
        body = line[1:] if line.startswith("+") else line  # unified-diff added lines
        for label, pat in _SHAPES:
            m = pat.search(body)
            if not m:
                continue
            value = m.group(1) if m.groups() else m.group(0)
            if _PLACEHOLDER.search(value) or _PLACEHOLDER.search(body):
                continue
            findings.append(f"line {lineno}: {label}")
            break
    return findings
