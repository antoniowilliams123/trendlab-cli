"""Structured outputs for the harness's own model calls (verifier, planner, screener, judge,
drafter, sleeptime): request the provider's JSON mode, validate, and repair once.

``json_messages`` marks a request so OpenAI-compatible providers send
``response_format={"type": "json_object"}`` and Ollama sends ``format="json"``. ``ask_json``
calls the model, validates with the caller's parser, and on failure asks once more with the
parser's complaint — a schema-validation repair loop instead of silently dropping the answer.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


def json_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [dict(m) for m in messages]
    if out:
        out[0]["_json_mode"] = True
    return out


async def ask_json[T](
    call: Callable[[list[dict[str, Any]]], Awaitable[str]],
    messages: list[dict[str, Any]],
    parse: Callable[[str], T | None],
) -> tuple[T | None, int]:
    """Returns (parsed or None, attempts used)."""
    first = await call(json_messages(messages))
    parsed = parse(first)
    if parsed is not None:
        return parsed, 1
    repair = json_messages(
        messages
        + [
            {"role": "assistant", "content": (first or "")[:2000]},
            {
                "role": "user",
                "content": "That was not valid JSON in the required shape. Reply again with "
                "ONLY the JSON object described above, nothing else.",
            },
        ]
    )
    second = await call(repair)
    return parse(second), 2
