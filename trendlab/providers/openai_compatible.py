"""OpenAI-compatible chat-completions provider (OpenAI, DeepSeek, Moonshot/Kimi, Ollama /v1)."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from trendlab.providers.base import (
    ModelProvider,
    ModelResponse,
    ProviderError,
    TokenUsage,
    ToolCall,
)


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
    ) -> None:
        self.name = provider_name
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._api_key_env = api_key_env
        self._client = client or httpx.AsyncClient(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key_env:
            key = os.environ.get(self._api_key_env)
            if not key:
                raise ProviderError(
                    f"{self.name}: environment variable {self._api_key_env} is not set"
                )
            headers["Authorization"] = f"Bearer {key}"
        return headers

    async def complete(self, messages, tools=None) -> ModelResponse:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
        try:
            resp = await self._client.post(
                f"{self._base_url}/chat/completions", json=payload, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name}: network error: {exc.__class__.__name__}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"{self.name}: HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
            choice = data["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError) as exc:
            raise ProviderError(f"{self.name}: malformed response") from exc
        tool_calls = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {"_raw": fn.get("arguments")}
            tool_calls.append(
                ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args)
            )
        usage = data.get("usage") or {}
        return ModelResponse(
            text=message.get("content") or "",
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
            ),
            finish_reason=choice.get("finish_reason"),
        )

    async def close(self) -> None:
        await self._client.aclose()
