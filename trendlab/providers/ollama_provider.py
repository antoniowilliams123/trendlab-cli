"""Native Ollama provider (spec §90.16): ``/api/chat`` with an explicit context window.

Why not Ollama's OpenAI-compatible ``/v1`` endpoint: it ignores the model's context length and
runs every request at the server default (2 048 tokens on 0.34), silently truncating the prompt
from the front. An agent whose system prompt, repository map and history are cut away loops and
hallucinates. The native endpoint takes ``options.num_ctx``, so TrendLab sends the model's real
window (``[models]`` entry, else what ``/api/show`` reports, capped by ``max_num_ctx``).

Everything else matches the OpenAI-compatible provider: tools, streaming (NDJSON), images,
normalized errors, usage mapped from ``prompt_eval_count`` / ``eval_count``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from trendlab.providers.base import (
    ModelCapabilities,
    ModelProvider,
    ModelResponse,
    ProviderContextOverflowError,
    ProviderError,
    ProviderMalformedResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    StreamChunk,
    TokenUsage,
    ToolCall,
)
from trendlab.ui.attachments import encode_image

DEFAULT_NUM_CTX = 32_768
MAX_NUM_CTX = 131_072


def _root(base_url: str) -> str:
    root = base_url.rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


class OllamaProvider(ModelProvider):
    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        provider_name: str = "ollama",
        client: httpx.AsyncClient | None = None,
        timeout: float = 300.0,
        context_window: int | None = None,
        max_num_ctx: int = MAX_NUM_CTX,
        native_tools: bool = True,
        keep_alive: str = "10m",
    ) -> None:
        self.name = provider_name
        self.model = model
        self._root = _root(base_url)
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._max_num_ctx = max_num_ctx
        self._num_ctx: int | None = min(context_window, max_num_ctx) if context_window else None
        self._keep_alive = keep_alive
        self._caps = ModelCapabilities(
            native_tools=native_tools,
            streaming=True,
            context_window=self._num_ctx,
            local=True,
        )

    def capabilities(self) -> ModelCapabilities:
        return self._caps

    # -- context window ----------------------------------------------------------------------------
    async def num_ctx(self) -> int:
        """The window to request: configured, else the model's own length from /api/show."""
        if self._num_ctx:
            return self._num_ctx
        try:
            resp = await self._client.post(f"{self._root}/api/show", json={"model": self.model})
            info = resp.json().get("model_info", {}) if resp.status_code < 400 else {}
            found = next((int(v) for k, v in info.items() if k.endswith("context_length")), None)
        except (httpx.HTTPError, ValueError, TypeError):
            found = None
        self._num_ctx = min(found or DEFAULT_NUM_CTX, self._max_num_ctx)
        self._caps = self._caps.model_copy(update={"context_window": self._num_ctx})
        return self._num_ctx

    # -- payload -----------------------------------------------------------------------------------
    def _messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            msg: dict[str, Any] = {"role": role}
            content = m.get("content")
            if isinstance(content, list):
                texts, images = [], []
                for part in content:
                    if part.get("type") == "image_path":
                        try:
                            _media, data = encode_image(part["path"])
                            images.append(data)
                        except ValueError as exc:
                            texts.append(f"[image unavailable: {exc}]")
                    elif part.get("type") == "text":
                        texts.append(part.get("text", ""))
                msg["content"] = "\n".join(texts)
                if images:
                    msg["images"] = images
            else:
                msg["content"] = content or ""
            if role == "assistant" and m.get("tool_calls"):
                msg["tool_calls"] = [
                    {
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": _as_dict(tc["function"].get("arguments")),
                        }
                    }
                    for tc in m["tool_calls"]
                ]
            if role == "tool":
                # Ollama matches tool results by order; keep the name when we have it.
                if m.get("name"):
                    msg["tool_name"] = m["name"]
            out.append(msg)
        return out

    async def _payload(self, messages, tools, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(messages),
            "stream": stream,
            "keep_alive": self._keep_alive,
            "options": {"num_ctx": await self.num_ctx()},
        }
        sampling = next((m["_sampling"] for m in messages if m.get("_sampling")), None)
        if sampling:  # per-role sampling policy (U18)
            payload["options"].update(
                {k: v for k, v in sampling.items() if k in {"temperature", "top_p"}}
            )
        if tools and self._caps.native_tools:
            payload["tools"] = tools
        elif any(m.get("_json_mode") for m in messages):
            payload["format"] = "json"  # structured output mode
        return payload

    def _raise_for_status(self, resp: httpx.Response, body: str | None = None) -> None:
        if resp.status_code < 400:
            return
        text = (body if body is not None else resp.text)[:300]
        low = text.lower()
        if resp.status_code == 404 and "not found" in low:
            raise ProviderError(
                f"{self.name}: model {self.model!r} is not pulled — run: ollama pull {self.model}"
            )
        if "context" in low and ("length" in low or "exceed" in low):
            raise ProviderContextOverflowError(f"{self.name}: context overflow: {text}")
        if resp.status_code >= 500:
            raise ProviderUnavailableError(f"{self.name}: HTTP {resp.status_code}: {text}")
        raise ProviderError(f"{self.name}: HTTP {resp.status_code}: {text}")

    # -- calls -------------------------------------------------------------------------------------
    async def complete(self, messages, tools=None) -> ModelResponse:
        try:
            resp = await self._client.post(
                f"{self._root}/api/chat", json=await self._payload(messages, tools, stream=False)
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"{self.name}: request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"{self.name}: is Ollama running at {self._root}? ({exc.__class__.__name__})"
            ) from exc
        self._raise_for_status(resp)
        try:
            data = resp.json()
            message = data["message"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderMalformedResponseError(f"{self.name}: malformed response") from exc
        return ModelResponse(
            text=message.get("content") or "",
            tool_calls=_tool_calls(message.get("tool_calls") or []),
            usage=_usage(data),
            finish_reason=data.get("done_reason"),
            raw_metadata={
                "model": data.get("model"),
                "thinking": message.get("thinking") or "",
                "provider_model": self.model,
                "num_ctx": self._num_ctx,
            },
        )

    async def stream(self, messages, tools=None) -> AsyncIterator[StreamChunk]:
        text_parts: list[str] = []
        think_parts: list[str] = []
        calls: list[dict[str, Any]] = []
        usage = TokenUsage()
        finish: str | None = None
        try:
            async with self._client.stream(
                "POST",
                f"{self._root}/api/chat",
                json=await self._payload(messages, tools, stream=True),
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    self._raise_for_status(resp, body)
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    msg = obj.get("message") or {}
                    if msg.get("thinking"):
                        think_parts.append(msg["thinking"])
                        yield StreamChunk(thinking=msg["thinking"])
                    if msg.get("content"):
                        text_parts.append(msg["content"])
                        yield StreamChunk(text=msg["content"])
                    calls.extend(msg.get("tool_calls") or [])
                    if obj.get("done"):
                        usage = _usage(obj)
                        finish = obj.get("done_reason")
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"{self.name}: stream timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"{self.name}: is Ollama running at {self._root}? ({exc.__class__.__name__})"
            ) from exc
        yield StreamChunk(
            final=ModelResponse(
                text="".join(text_parts),
                tool_calls=_tool_calls(calls),
                usage=usage,
                finish_reason=finish,
                raw_metadata={
                    "thinking": "".join(think_parts),
                    "provider_model": self.model,
                    "num_ctx": self._num_ctx,
                },
            )
        )

    async def close(self) -> None:
        await self._client.aclose()


def _as_dict(args: Any) -> dict[str, Any]:
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args or "{}")
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except ValueError:
            return {"_raw": args}
    return {}


def _tool_calls(raw: list[dict[str, Any]]) -> list[ToolCall]:
    calls = []
    for tc in raw:
        fn = tc.get("function") or {}
        calls.append(
            ToolCall(
                id=tc.get("id") or "call_" + uuid.uuid4().hex[:10],
                name=fn.get("name") or "",
                arguments=_as_dict(fn.get("arguments")),
            )
        )
    return calls


def _usage(data: dict[str, Any]) -> TokenUsage:
    return TokenUsage(
        input_tokens=int(data.get("prompt_eval_count") or 0),
        output_tokens=int(data.get("eval_count") or 0),
        cached_input_tokens=0,
    )
