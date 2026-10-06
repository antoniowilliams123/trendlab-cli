"""Small rules shared by the TUI prompt and the plain REPL (no Textual import needed)."""

from __future__ import annotations


def continues_line(text: str) -> bool:
    """True when the prompt ends with a continuation backslash: ``foo \\`` or a lone ``\\``.

    A backslash glued to a word (a pasted Windows path like ``C:\\x\\results\\``) is just text
    and must not swallow the Enter key.
    """
    stripped = text.rstrip()
    if not stripped.endswith("\\"):
        return False
    before = stripped[:-1]
    return before == "" or before.endswith((" ", "\t", "\n"))
