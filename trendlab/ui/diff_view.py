"""Unified diff generation and Rich rendering (spec §30)."""

from __future__ import annotations

import difflib

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text


def unified_diff(path: str, old: str, new: str, context: int = 3) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=context,
    )
    return "".join(lines)


def diff_stats(diff: str) -> tuple[int, int]:
    lines = diff.splitlines()
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    return added, removed


def render_diff(diff: str, title: str | None = None, max_lines: int = 400) -> Panel:
    text = Text()
    lines = diff.splitlines()
    for line in lines[:max_lines]:
        if line.startswith("+++") or line.startswith("---"):
            text.append(line + "\n", style="bold #39ff14")
        elif line.startswith("@@"):
            text.append(line + "\n", style="#00ffd0")
        elif line.startswith("+"):
            text.append(line + "\n", style="#39ff14 on #062406")
        elif line.startswith("-"):
            text.append(line + "\n", style="#ff3b3b on #240606")
        else:
            text.append(line + "\n", style="#5c6e5a")
    if len(lines) > max_lines:
        text.append(f"... {len(lines) - max_lines} more lines\n", style="dim italic")
    added, removed = diff_stats(diff)
    return Panel(
        Group(text),
        title=title or f"diff  [#39ff14]+{added}[/#39ff14] [#ff3b3b]-{removed}[/#ff3b3b]",
        border_style="#1f9e12",
        title_align="left",
    )


def print_diff(console: Console, diff: str, title: str | None = None) -> None:
    console.print(render_diff(diff, title))
