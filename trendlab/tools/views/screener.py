"""Screener prompt (cheap-model spec §3.1): ≤300-token instruction, strict JSON answer."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from trendlab.tools.views import ToolOutput

MAX_INPUT_CHARS = 24_000  # head + tail of the raw text handed to the screener

PROMPT = """You condense raw tool output for another model that must not read it all.
Return ONLY a JSON object: {{"tier1": "...", "tier2": "..."}}.
tier1: at most 4 short lines of FACTS (counts, exit code, the first error with file:line,
what succeeded). No advice, no guesses, no markdown.
tier2: at most 25 lines of the most decision-relevant detail copied verbatim (error lines,
failing names, key values). Empty string if nothing matters beyond tier1.
Tool: {tool}{command}
Raw output ({chars} chars, {lines} lines; middle may be elided):
<<<
{body}
>>>"""

_JSON = re.compile(r"\{.*\}", re.S)


def build_messages(text: str, meta: dict[str, Any]) -> list[dict[str, Any]]:
    body = text
    if len(body) > MAX_INPUT_CHARS:
        half = MAX_INPUT_CHARS // 2
        body = body[:half] + "\n… [elided] …\n" + body[-half:]
    command = f" — {meta['command'][:200]}" if meta.get("command") else ""
    content = PROMPT.format(
        tool=meta.get("tool", "shell"),
        command=command,
        chars=len(text),
        lines=text.count("\n") + 1,
        body=body,
    )
    return [{"role": "user", "content": content}]


def parse_answer(answer: str) -> ToolOutput | None:
    m = _JSON.search(answer or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    tier1 = str(obj.get("tier1") or "").strip()
    if not tier1:
        return None
    tier1 = "\n".join(tier1.splitlines()[:4])[:600]
    tier2 = "\n".join(str(obj.get("tier2") or "").splitlines()[:25])[:3000].strip() or None
    return ToolOutput(tier1=tier1, tier2=tier2, kind="text", parser="screener")


async def screen(
    call: Callable[[list[dict[str, Any]]], Awaitable[str]], text: str, meta: dict[str, Any]
) -> ToolOutput | None:
    return parse_answer(await call(build_messages(text, meta)))
