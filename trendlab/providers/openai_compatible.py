"""OpenAI-compatible chat-completions provider (OpenAI, DeepSeek, Moonshot/Kimi, Ollama /v1).

Handles non-streaming and SSE streaming, maps HTTP failures onto normalized
error classes, and never leaks vendor JSON past this module.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from trendlab.providers.base import (
    ModelCapabilities,
    ModelProvider,
    ModelResponse,
    ProviderAuthenticationError,
    ProviderContextOverflowError,
    ProviderError,
    ProviderMalformedResponseError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    StreamChunk,
    TokenUsage,
    ToolCall,
)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}


def _is_local(base_url: str) -> bool:
    host = httpx.URL(base_url).host
    return host in _LOCAL_HOSTS or host.endswith(".local") or host.startswith("192.168.")


class OpenAICompatibleProvider(ModelProvider):
    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key_env: str | None,
        provider_name: str = "openai_compatible",
        client: httpx.AsyncClient | None = None,
        timeout: float = 120.0,
        context_window: int | None = None,
        native_tools: bool = True,
        local: bool | None = None,
    ) -> None:
        self.name = provider_name
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._api_key_env = api_key_env
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._caps = ModelCapabilities(
            native_tools=native_tools,
            streaming=True,
            context_window=context_window,
            local=_is_local(base_url) if local is None else local,
        )

    def capabilities(self) -> ModelCapabilities:
        return self._caps

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key_env:
            key = os.environ.get(self._api_key_env)
            if not key:
                raise ProviderAuthenticationError(
                    f"{self.name}: environment variable {self._api_key_env} is not set"
                )
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _payload(self, messages, tools, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools and self._caps.native_tools:
            payload["tools"] = tools
        if stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        body = resp.text[:300]
        if resp.status_code in {401, 403}:
            raise ProviderAuthenticationError(f"{self.name}: HTTP {resp.status_code}: {body}")
        if resp.status_code == 429:
            raise ProviderRateLimitError(f"{self.name}: rate limited: {body}")
        if resp.status_code == 408:
            raise ProviderTimeoutError(f"{self.name}: HTTP 408")
        if resp.status_code == 400 and any(
            k in body.lower()
            for k in (
                "context length",
                "context_length",
                "too many tokens",
                "maximum context",
                "prompt is too long",
            )
        ):
            raise ProviderContextOverflowError(f"{self.name}: context overflow: {body}")
        if resp.status_code >= 500:
            raise ProviderUnavailableError(f"{self.name}: HTTP {resp.status_code}: {body}")
        raise ProviderError(f"{self.name}: HTTP {resp.status_code}: {body}")

    async def complete(self, messages, tools=None) -> ModelResponse:
        try:
            resp = await self._client.post(
                f"{self._base_url}/chat/completions",
                json=self._payload(messages, tools, stream=False),
                headers=self._headers(),
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"{self.name}: request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"{self.name}: network error: {exc.__class__.__name__}"
            ) from exc
        self._raise_for_status(resp)
        try:
            data = resp.json()
            choice = data["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderMalformedResponseError(f"{self.name}: malformed response") from exc
        return ModelResponse(
            text=message.get("content") or "",
            tool_calls=_parse_tool_calls(message.get("tool_calls") or []),
            usage=_parse_usage(data.get("usage")),
            finish_reason=choice.get("finish_reason"),
            raw_metadata={"id": data.get("id"), "model": data.get("model")},
        )

    async def stream(self, messages, tools=None) -> AsyncIterator[StreamChunk]:
        text_parts: list[str] = []
        pending_calls: dict[int, dict[str, Any]] = {}
        usage = TokenUsage()
        finish: str | None = None
        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                json=self._payload(messages, tools, stream=True),
                headers=self._headers(),
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    self._raise_for_status(resp)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue  # malformed chunk: skip rather than crash (spec §73)
                    if obj.get("usage"):
                        usage = _parse_usage(obj["usage"])
                    for choice in obj.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            text_parts.append(delta["content"])
                            yield StreamChunk(text=delta["content"])
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            slot = pending_calls.setdefault(
                                idx, {"id": "", "name": "", "arguments": ""}
                            )
                            slot["id"] = tc.get("id") or slot["id"]
                            fn = tc.get("function") or {}
                            slot["name"] = fn.get("name") or slot["name"]
                            slot["arguments"] += fn.get("arguments") or ""
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"{self.name}: stream timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"{self.name}: network error: {exc.__class__.__name__}"
            ) from exc
        calls = [
            {
                "id": c["id"] or f"call_{i}",
                "function": {"name": c["name"], "arguments": c["arguments"]},
            }
            for i, c in sorted(pending_calls.items())
        ]
        yield StreamChunk(
            final=ModelResponse(
                text="".join(text_parts),
                tool_calls=_parse_tool_calls(calls),
                usage=usage,
                finish_reason=finish,
            )
        )

    async def close(self) -> None:
        await self._client.aclose()


def _parse_tool_calls(raw: list[dict[str, Any]]) -> list[ToolCall]:
    calls = []
    for tc in raw:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {"_malformed_json": fn.get("arguments")}
        if not isinstance(args, dict):
            args = {"_malformed_json": args}
        calls.append(ToolCall(id=tc.get("id") or "", name=fn.get("name") or "", arguments=args))
    return calls


def _parse_usage(usage: dict[str, Any] | None) -> TokenUsage:
    usage = usage or {}
    details = usage.get("prompt_tokens_details") or {}
    return TokenUsage(
        input_tokens=usage.get("prompt_tokens", 0) or 0,
        output_tokens=usage.get("completion_tokens", 0) or 0,
        cached_input_tokens=details.get("cached_tokens", 0) or 0,
    )
