"""Rescue tool calls that a model wrote as text (spec §90.15).

Smaller and local models sometimes answer with the *JSON of a tool call* instead of issuing the
call: ``{"name": "list_directory", "arguments": {"path": "."}}``. Left alone, that text is treated
as the final answer and the run "completes" having done nothing. This module recognises the common
shapes and turns them into real ``ToolCall`` objects so the loop executes them.

Accepted shapes (fenced or bare, one object or a list, optionally wrapped in ``tool_calls``):
``{"name"|"action"|"tool"|"function": X, "arguments"|"parameters"|"params"|"input"|"args": {...}}``
and OpenAI's ``{"function": {"name": X, "arguments": {...}|"<json string>"}}``. Only known tool
names are rescued, and only when the reply is essentially the call (little prose around it).
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from trendlab.providers.base import ToolCall

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)\s*```", re.S)
_NAME_KEYS = ("name", "action", "tool", "tool_name", "function_name")
_ARG_KEYS = ("arguments", "parameters", "params", "input", "args")
MAX_PROSE_CHARS = 240  # more than this around the JSON → the model was explaining, not calling


def _json_objects(text: str) -> list[tuple[Any, int, int]]:
    """Every top-level JSON value in ``text`` as ``(value, start, end)``."""
    dec = json.JSONDecoder()
    out: list[tuple[Any, int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "{[":
            try:
                value, end = dec.raw_decode(text, i)
            except ValueError:
                i += 1
                continue
            out.append((value, i, end))
            i = end
        else:
            i += 1
    return out


def _as_call(item: Any, known: set[str]) -> ToolCall | None:
    if not isinstance(item, dict):
        return None
    name: Any = None
    args: Any = None
    fn = item.get("function")
    if isinstance(fn, dict):
        name = fn.get("name")
        args = next((fn[k] for k in _ARG_KEYS if k in fn), {})
    else:
        name = next((item[k] for k in _NAME_KEYS if k in item), None)
        args = next((item[k] for k in _ARG_KEYS if k in item), None)
    if not isinstance(name, str) or name not in known:
        return None
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None
    return ToolCall(id="call_" + uuid.uuid4().hex[:10], name=name, arguments=args)


def rescue_text_tool_calls(text: str | None, known_tools: list[str]) -> list[ToolCall]:
    """Tool calls the model wrote as text, or ``[]`` when the text is a real answer."""
    if not text or "{" not in text:
        return []
    known = set(known_tools)
    body = text
    fenced = _FENCE.findall(text)
    if fenced:
        body = "\n".join(fenced)
    found = _json_objects(body)
    if not found:
        return []
    calls: list[ToolCall] = []
    spans: list[tuple[int, int]] = []
    for value, start, end in found:
        items: list[Any]
        if isinstance(value, dict) and isinstance(value.get("tool_calls"), list):
            items = value["tool_calls"]
        elif isinstance(value, list):
            items = value
        else:
            items = [value]
        batch = [c for c in (_as_call(it, known) for it in items) if c is not None]
        if batch:
            calls.extend(batch)
            spans.append((start, end))
    if not calls:
        return []
    # Prose outside the JSON (and outside fences) must be short: a call, not an explanation.
    prose = body
    for start, end in sorted(spans, reverse=True):
        prose = prose[:start] + prose[end:]
    if fenced:
        outside = _FENCE.sub("", text)
        prose += outside
    if len(prose.strip()) > MAX_PROSE_CHARS:
        return []
    return calls
