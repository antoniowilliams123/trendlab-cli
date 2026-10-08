"""Issue drafter (cheap-model spec §3.4): a cheap-model role turns a trace into a diagnosis
card used by `/inbox open`, Telegram digests and `/pr`."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

CARD_FIELDS = (
    "problem",
    "root_cause",
    "impacted_files",
    "evidence_refs",
    "proposed_change",
    "test_results",
)
PROMPT = """You write a short diagnosis card from an agent trace. Answer with ONE JSON object:
{{"problem": "one sentence", "root_cause": "one or two sentences, or 'unknown'",
 "impacted_files": ["path", "..."], "evidence_refs": ["session or log ids / file:line"],
 "proposed_change": "concrete, minimal", "test_results": "what the trace shows about tests"}}
Facts only; say 'unknown' rather than guessing.

## Title
{title}

## Trace (may be truncated)
{trace}
"""
_JSON = re.compile(r"\{.*\}", re.S)
MAX_TRACE = 12_000


def build_messages(title: str, trace: str) -> list[dict[str, Any]]:
    if len(trace) > MAX_TRACE:
        trace = trace[: MAX_TRACE // 2] + "\n… [elided] …\n" + trace[-MAX_TRACE // 2 :]
    return [{"role": "user", "content": PROMPT.format(title=title[:200], trace=trace)}]


def parse_card(text: str) -> dict[str, Any] | None:
    m = _JSON.search(text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    card: dict[str, Any] = {}
    for k in CARD_FIELDS:
        v = obj.get(k)
        if k in {"impacted_files", "evidence_refs"}:
            card[k] = [str(x)[:200] for x in (v or []) if isinstance(x, str | int)][:12]
        else:
            card[k] = str(v or "unknown")[:600]
    return card if card.get("problem") and card["problem"] != "unknown" else None


async def draft_card(
    call: Callable[[list[dict[str, Any]]], Awaitable[str]], title: str, trace: str
) -> dict[str, Any] | None:
    return parse_card(await call(build_messages(title, trace)))


def render_card(card: dict[str, Any]) -> str:
    lines = [f"Problem: {card.get('problem', '')}", f"Root cause: {card.get('root_cause', '')}"]
    if card.get("impacted_files"):
        lines.append("Files: " + ", ".join(card["impacted_files"]))
    if card.get("evidence_refs"):
        lines.append("Evidence: " + ", ".join(card["evidence_refs"]))
    lines.append(f"Proposed change: {card.get('proposed_change', '')}")
    lines.append(f"Tests: {card.get('test_results', '')}")
    return "\n".join(lines)
