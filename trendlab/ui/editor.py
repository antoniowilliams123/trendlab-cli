"""External editor support: write the draft to a temp file, open $EDITOR, read it back."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path


def editor_command() -> list[str] | None:
    for var in ("VISUAL", "EDITOR"):
        value = os.environ.get(var)
        if value:
            return shlex.split(value)
    for candidate in ("nano", "vim", "vi", "notepad.exe"):
        if shutil.which(candidate):
            return [candidate]
    return None


def edit_in_external_editor(initial: str = "", *, suffix: str = ".md") -> str | None:
    """Open the editor on ``initial``; return the edited text, or None if no editor is available."""
    cmd = editor_command()
    if cmd is None:
        return None
    fd, path = tempfile.mkstemp(prefix="trendlab-prompt-", suffix=suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(initial)
        subprocess.run([*cmd, path], check=False)  # noqa: S603 — user's own editor
        return Path(path).read_text(encoding="utf-8")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
