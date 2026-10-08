"""Tiered tool output (cheap-model spec §2): agent-native views instead of terminal dumps.

Every large tool result becomes a ``ToolOutput``:

* ``tier1`` — a few lines of *facts* (counts, exit code, duration, the first failing id); what
  the lead model always sees;
* ``tier2`` — targeted detail (failing tests with their assertion lines, top matches, newest
  entries); included when it fits the tool's budget;
* ``tier3_ref`` — the full text, written to ``.trendlab/traces/<call_id>.log``; never inserted
  into the prompt unasked (the ``inspect_output`` tool pulls slices on demand).

Parsers are pure functions over text. When none applies, a generic summary is used and, above
the screener threshold, a cheap model is asked to produce the tiers (runtime wiring).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from trendlab.tools.views import (
    gitview,
    http,
    lint,
    listing,
    search,
    tests_js,
    tests_py,
    tests_rs_go,
)


class ToolOutput(BaseModel):
    tier1: str
    tier2: str | None = None
    tier3_ref: str | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
    kind: str = "text"
    parser: str = "generic"

    def render(self, budget_chars: int) -> str:
        """Tier 1, then Tier 2 while it fits the budget, then a pointer to Tier 3."""
        parts = [self.tier1.strip()]
        remaining = budget_chars - len(parts[0])
        if self.tier2 and remaining > 80:
            t2 = self.tier2.strip()
            if len(t2) > remaining:
                t2 = t2[: remaining - 40].rstrip() + "\n… (more via inspect_output)"
            parts.append(t2)
        if self.tier3_ref:
            parts.append(
                f'[full output: inspect_output(call_id="{self.stats.get("call_id", "")}")'
                f" · {self.stats.get('bytes', 0)} bytes, {self.stats.get('lines', 0)} lines]"
            )
        return "\n".join(p for p in parts if p)


Parser = Callable[[str, dict[str, Any]], ToolOutput | None]

# Order matters: the first parser that recognises the text wins.
PARSERS: list[tuple[str, Parser]] = [
    ("tests_py", tests_py.parse),
    ("tests_js", tests_js.parse),
    ("tests_rs_go", tests_rs_go.parse),
    ("lint", lint.parse),
    ("gitview", gitview.parse),
    ("search", search.parse),
    ("listing", listing.parse),
    ("http", http.parse),
]

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def clean(text: str) -> str:
    return _ANSI.sub("", text or "").replace("\r\n", "\n").replace("\r", "\n")


def generic(text: str, meta: dict[str, Any]) -> ToolOutput:
    """Fallback: counts plus a head and tail that together stay small."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    n = len(lines)
    exit_code = meta.get("exit_code")
    head = [f"{n} lines, {len(text)} chars"]
    if exit_code is not None:
        head[0] += f", exit {exit_code}"
    if meta.get("duration_ms") is not None:
        head[0] += f", {meta['duration_ms'] / 1000:.1f}s"
    errors = [
        ln for ln in lines if re.search(r"\b(error|exception|traceback|failed|fatal)\b", ln, re.I)
    ]
    if errors:
        head.append(f"{len(errors)} error-like lines; first: {errors[0][:160]}")
    tier2 = None
    if n:
        take = 12
        if n <= 2 * take:
            tier2 = "\n".join(lines)
        else:
            tier2 = (
                "\n".join(lines[:take])
                + f"\n… ({n - 2 * take} lines omitted) …\n"
                + "\n".join(lines[-take:])
            )
    return ToolOutput(
        tier1="\n".join(head),
        tier2=tier2,
        kind="text",
        parser="generic",
        stats={"lines": n, "bytes": len(text)},
    )


def tier_output(text: str, meta: dict[str, Any]) -> ToolOutput:
    """Pick a parser by content (and ``meta['hint']`` when the tool knows its own kind)."""
    text = clean(text)
    hint = meta.get("hint")
    ordered = PARSERS
    if hint:
        ordered = [p for p in PARSERS if p[0] == hint] + [p for p in PARSERS if p[0] != hint]
    for name, parser in ordered:
        try:
            out = parser(text, meta)
        except Exception:  # noqa: BLE001 — a parser bug must never break a tool result
            out = None
        if out is not None:
            out.parser = name
            out.stats.setdefault("lines", len(text.splitlines()))
            out.stats.setdefault("bytes", len(text))
            return out
    return generic(text, meta)


__all__ = ["ToolOutput", "tier_output", "generic", "clean", "PARSERS"]
