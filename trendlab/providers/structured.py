"""Structured-JSON tool-calling fallback (spec §59).

Wraps any provider whose model cannot emit native function calls reliably.
Tools are described in the system prompt; the model answers with a JSON
object ``{"action": name, "arguments": {...}}`` (or a list of them) inside a
fenced block. Malformed output becomes a corrective observation, never a
partially parsed command.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from trendlab.providers.base import ModelCapabilities, ModelProvider, ModelResponse, ToolCall
from trendlab.ui.attachments import text_of

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)
_DONE_MARKER = "FINAL:"

_INSTRUCTIONS = """
TOOL PROTOCOL (this model uses structured JSON instead of native function calls).
When you need a tool, reply with ONLY a fenced JSON block:
```json
{"action": "<tool name>", "arguments": { ... }}
```
You may send a JSON list of several such objects to call tools in sequence.
When the task is finished, reply in plain text starting with "FINAL:" followed by your report.
Available tools:
"""


class StructuredToolProvider(ModelProvider):
    def __init__(self, inner: ModelProvider) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = inner.model

    def capabilities(self) -> ModelCapabilities:
        caps = self.inner.capabilities().model_copy()
        caps.native_tools = False
        caps.parallel_tool_calls = False
        return caps

    async def complete(self, messages, tools=None) -> ModelResponse:
        response = await self.inner.complete(_rewrite(messages, tools), None)
        return _parse(response, tools)

    async def close(self) -> None:
        await self.inner.close()


def _tool_catalog(tools: list[dict[str, Any]] | None) -> str:
    lines = []
    for t in tools or []:
        fn = t.get("function", t)
        params = fn.get("parameters", {}).get("properties", {})
        sig = ", ".join(f"{k}: {v.get('type', 'any')}" for k, v in params.items())
        lines.append(f"- {fn['name']}({sig}): {fn.get('description', '')}")
    return "\n".join(lines)


def _rewrite(messages: list[dict[str, Any]], tools) -> list[dict[str, Any]]:
    """Fold tool definitions into the system prompt and tool messages into user turns."""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            out.append(
                {
                    "role": "system",
                    "content": f"{m['content']}\n{_INSTRUCTIONS}{_tool_catalog(tools)}",
                }
            )
        elif role == "assistant" and m.get("tool_calls"):
            calls = [
                {
                    "action": c["function"]["name"],
                    "arguments": json.loads(c["function"]["arguments"] or "{}"),
                }
                for c in m["tool_calls"]
            ]
            out.append({"role": "assistant", "content": "```json\n" + json.dumps(calls) + "\n```"})
        elif role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": f"[tool result for {m.get('name')}]\n{m.get('content', '')}",
                }
            )
        else:
            out.append({"role": role, "content": text_of(m.get("content"))})
    if not any(m["role"] == "system" for m in out):
        out.insert(0, {"role": "system", "content": _INSTRUCTIONS + _tool_catalog(tools)})
    return out


def _parse(response: ModelResponse, tools) -> ModelResponse:
    text = response.text or ""
    stripped = text.strip()
    if stripped.startswith(_DONE_MARKER):
        return response.model_copy(
            update={"text": stripped[len(_DONE_MARKER) :].strip(), "tool_calls": []}
        )
    match = _FENCE.search(text)
    if match:
        candidate = match.group(1).strip()
    elif stripped[:1] in "{[":
        candidate = stripped
    else:
        return response  # plain prose: treat as final answer
    try:
        parsed = json.loads(candidate)
    except ValueError as exc:
        return response.model_copy(
            update={
                "text": "",
                "tool_calls": [
                    ToolCall(
                        id=_cid(),
                        name="_malformed",
                        arguments={"error": f"invalid JSON: {exc}", "raw": candidate[:500]},
                    )
                ],
            }
        )
    items = parsed if isinstance(parsed, list) else [parsed]
    known = {t.get("function", t)["name"] for t in tools or []}
    calls = []
    for item in items:
        if not isinstance(item, dict) or "action" not in item:
            calls.append(
                ToolCall(
                    id=_cid(),
                    name="_malformed",
                    arguments={"error": "each item needs an 'action'", "raw": str(item)[:300]},
                )
            )
            continue
        args = item.get("arguments", {})
        if not isinstance(args, dict):
            calls.append(
                ToolCall(
                    id=_cid(),
                    name="_malformed",
                    arguments={"error": "'arguments' must be an object"},
                )
            )
            continue
        name = str(item["action"])
        if known and name not in known:
            calls.append(
                ToolCall(
                    id=_cid(),
                    name="_malformed",
                    arguments={"error": f"unknown tool {name!r}", "available": sorted(known)},
                )
            )
            continue
        calls.append(ToolCall(id=_cid(), name=name, arguments=args))
    return response.model_copy(update={"text": "", "tool_calls": calls})


def _cid() -> str:
    return "call_" + uuid.uuid4().hex[:10]
