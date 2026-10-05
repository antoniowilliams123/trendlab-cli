"""Classify shell commands into operation categories.

Conservative by design: anything unrecognized is SHELL_WRITE (asks in ``ask``
mode). Destructive and privileged patterns are detected first so a command such
as ``ls && rm -rf /`` is classified by its most dangerous segment.
"""

from __future__ import annotations

import re
import shlex

from trendlab.permissions.models import OperationCategory

_PRIVILEGED = re.compile(r"(^|\s|;|&&|\|\|)\s*(sudo|su|doas|pkexec)(\s|$)")
_DESTRUCTIVE_PATTERNS = [
    re.compile(r"\brm\s+(-[a-zA-Z]*[rRf][a-zA-Z]*\s+)+"),  # rm -rf, rm -r, rm -f
    re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-[a-zA-Z]*[fdx]|push\s+.*--force|push\s+-f\b)"),
    re.compile(r"\bgit\s+branch\s+-D\b"),
    re.compile(r"\b(mkfs|fdisk|dd\s+if=|shred|wipefs)\b"),
    re.compile(r"\bchmod\s+(-R\s+)?[0-7]{3,4}\s+/(\s|$)"),
    re.compile(r"\bchown\s+-R\b"),
    re.compile(r"(?i)\bdrop\s+(database|table)\b"),
    re.compile(r"\btruncate\s+"),
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"),  # fork bomb
    re.compile(r">\s*/dev/sd[a-z]"),
]
_PACKAGE_INSTALL = re.compile(
    r"\b(pip3?|uv|pipx|poetry|conda)\s+(install|add)\b|\b(npm|pnpm|yarn)\s+(install|add|i)\b"
    r"|\bapt(-get)?\s+install\b|\bbrew\s+install\b|\bcargo\s+(install|add)\b|\bgo\s+get\b"
)
_NETWORK = re.compile(
    r"\b(curl|wget|ssh|scp|rsync|nc|ncat|telnet|ftp|git\s+(push|fetch|pull|clone))\b"
)
_TESTS = re.compile(
    r"^\s*(python3?\s+-m\s+)?(pytest|unittest|tox|nox|jest|vitest|mocha|cargo\s+test|go\s+test|"
    r"npm\s+test|pnpm\s+test|yarn\s+test|ruff|mypy|pyright|flake8|eslint|black\s+--check)\b"
)
_READ_ONLY_CMDS = {
    "pwd",
    "ls",
    "cat",
    "head",
    "tail",
    "less",
    "more",
    "wc",
    "echo",
    "printf",
    "env",
    "which",
    "type",
    "file",
    "stat",
    "find",
    "grep",
    "rg",
    "ag",
    "fd",
    "tree",
    "du",
    "df",
    "date",
    "whoami",
    "uname",
    "python3 --version",
    "python --version",
    "node --version",
}
_GIT_READ = re.compile(
    r"^\s*git\s+(status|diff|log|show|branch|rev-parse|remote\s+-v|ls-files|blame)\b"
)

_SHELL_SPLIT = re.compile(r"\s*(?:\|\||&&|;|\|)\s*")
_REDIRECT_WRITE = re.compile(r"(^|[^<>])>{1,2}\s*\S")


def _segments(command: str) -> list[str]:
    return [seg for seg in _SHELL_SPLIT.split(command.strip()) if seg]


def _is_read_only_segment(seg: str) -> bool:
    if _GIT_READ.match(seg):
        return True
    if _REDIRECT_WRITE.search(seg):
        return False
    try:
        parts = shlex.split(seg)
    except ValueError:
        return False
    if not parts:
        return True
    if parts[0] in _READ_ONLY_CMDS:
        return True
    if parts[0] == "python3" and len(parts) >= 2 and parts[1] == "--version":
        return True
    return False


def classify_shell_command(command: str) -> OperationCategory:
    text = command.strip()
    if not text:
        return OperationCategory.SHELL_READ
    if _PRIVILEGED.search(" " + text):
        return OperationCategory.PRIVILEGED
    for pat in _DESTRUCTIVE_PATTERNS:
        if pat.search(text):
            return OperationCategory.DESTRUCTIVE
    if _PACKAGE_INSTALL.search(text):
        return OperationCategory.PACKAGE_INSTALL
    if _NETWORK.search(text):
        return OperationCategory.NETWORK
    segments = _segments(text)
    if segments and all(_TESTS.match(s) for s in segments):
        return OperationCategory.RUN_TESTS
    if segments and all(_is_read_only_segment(s) for s in segments):
        return OperationCategory.SHELL_READ
    return OperationCategory.SHELL_WRITE
