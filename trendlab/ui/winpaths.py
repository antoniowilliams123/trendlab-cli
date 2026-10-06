"""Windows → WSL path translation for prompts (spec §90.14).

On WSL people paste paths straight from Explorer or VS Code: ``\\\\wsl$\\Ubuntu\\home\\tony\\x``,
``\\\\wsl.localhost\\Ubuntu\\home\\tony\\x`` or ``C:\\Users\\tony\\x``. The Linux tools cannot
open those, and weaker models refuse instead of translating. Translate them before the model sees
the prompt so ``/home/tony/x`` and ``/mnt/c/Users/tony/x`` arrive instead.
"""

from __future__ import annotations

import re
from pathlib import Path

_WSL_UNC = re.compile(
    r"(?:\\\\|//)wsl(?:\$|\.localhost)[\\/][^\\/\s]+((?:[\\/][^\\/\s\"'<>|]+)+)", re.I
)
_DRIVE = re.compile(r"(?<![\w/\\])([A-Za-z]):[\\/]((?:[^\\/\s\"'<>|:]+[\\/]?)*)")


def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def translate_windows_paths(text: str, *, force: bool = False) -> tuple[str, int]:
    """Return ``(text, count)`` with Windows paths rewritten to their WSL equivalents.

    Only active on WSL (or with ``force``) — on a real Windows host those paths are correct.
    """
    if not (force or is_wsl()):
        return text, 0
    count = 0

    def unc(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return m.group(1).replace("\\", "/")

    def drive(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        rest = m.group(2).replace("\\", "/").rstrip("/")
        return f"/mnt/{m.group(1).lower()}/{rest}" if rest else f"/mnt/{m.group(1).lower()}"

    text = _WSL_UNC.sub(unc, text)
    text = _DRIVE.sub(drive, text)
    return text, count
