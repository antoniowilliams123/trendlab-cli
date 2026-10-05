"""Anthropic Messages API provider via the official ``anthropic`` SDK.

Translates TrendLab's OpenAI-shaped internal history (system / user / assistant with
``tool_calls`` / ``tool`` results) into Anthropic's content-block format and back. When the
same Anthropic model continues a conversation, the assistant turn's original content blocks
(including thinking blocks) are replayed unchanged via ``_provider_content``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

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
from trendlab.security.secrets import resolve_secret

DEFAULT_MODEL = "claude-opus-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_ADAPTIVE_FAMILIES = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
)
_FALLBACK_FAMILIES = ("claude-fable-5", "claude-opus-5")


def thinking_param(model: str, mode: str) -> dict[str, Any] | None:
    """Adaptive thinking on 4.6+ families; nothing for Haiku/older (they need budget_tokens)."""
    if mode == "off":
        return None
    if mode == "adaptive" or any(model.startswith(f) for f in _ADAPTIVE_FAMILIES):
        return {"type": "adaptive"}
    return None


def fallbacks_enabled(model: str, mode: str) -> bool:
    if mode == "on":
        return True
    if mode == "off":
        return False
    return any(model.startswith(f) for f in _FALLBACK_FAMILIES) and not model.startswith(
        "claude-opus-5-5"
    )


def translate_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for t in tools or []:
        fn = t.get("function", t)
        out.append(
            {
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return out


def translate_messages(
    messages: list[dict[str, Any]], model: str
) -> tuple[str | None, list[dict[str, Any]]]:
    """Return (system_text, anthropic_messages)."""
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        if pending_results:
            out.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for m in messages:
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                system_parts.append(str(m["content"]))
            continue
        if role == "tool":
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": str(m.get("tool_call_id", "")),
                    "content": str(m.get("content") or ""),
                }
            )
            continue
        flush_results()
        if role == "user":
            text = m.get("content")
            if isinstance(text, list):
                out.append({"role": "user", "content": text})
            elif text:
                out.append({"role": "user", "content": [{"type": "text", "text": str(text)}]})
            continue
        if role == "assistant":
            if m.get("_provider_content") and m.get("_provider_model") == model:
                out.append({"role": "assistant", "content": m["_provider_content"]})
                continue
            blocks: list[dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": str(m["content"])})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id") or "call_0",
                        "name": fn.get("name", ""),
                        "input": args if isinstance(args, dict) else {},
                    }
                )
            if blocks:
                out.append({"role": "assistant", "content": blocks})
    flush_results()
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "Continue."}]})
    return ("\n\n".join(system_parts) or None), out


class AnthropicProvider(ModelProvider):
    name = "anthropic"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key_env: str | None = "ANTHROPIC_API_KEY",
        provider_name: str = "anthropic",
        max_tokens: int = 16000,
        thinking: str = "auto",
        effort: str | None = None,
        refusal_fallbacks: str = "auto",
        cache: bool = True,
        timeout: float = 600.0,
        context_window: int | None = None,
        client: Any = None,
    ) -> None:
        self.name = provider_name
        self.model = model
        self._api_key_env = api_key_env
        self._max_tokens = max_tokens
        self._thinking = thinking
        self._effort = effort
        self._fallbacks = refusal_fallbacks
        self._cache = cache
        self._timeout = timeout
        self._client = client
        self._caps = ModelCapabilities(
            native_tools=True,
            streaming=True,
            parallel_tool_calls=True,
            context_window=context_window or (200_000 if "haiku" in model else 1_000_000),
            local=False,
        )

    def capabilities(self) -> ModelCapabilities:
        return self._caps

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - dependency declared in pyproject
                raise ProviderError("the 'anthropic' package is not installed") from exc
            key = resolve_secret(self._api_key_env)
            # Without a key the SDK still resolves `ant auth login` profiles / WIF on its own.
            self._client = (
                anthropic.AsyncAnthropic(api_key=key, timeout=self._timeout)
                if key
                else anthropic.AsyncAnthropic(timeout=self._timeout)
            )
        return self._client

    def _params(self, messages, tools) -> dict[str, Any]:
        system, msgs = translate_messages(messages, self.model)
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "messages": msgs,
        }
        if system:
            # Prompt caching: the system prompt is the stable prefix; mark it as a breakpoint.
            params["system"] = (
                [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
                if self._cache
                else system
            )
        anth_tools = translate_tools(tools)
        if anth_tools:
            params["tools"] = anth_tools
        if self._cache and msgs:
            # Second breakpoint on the latest turn so the growing history is served from cache
            # on the next call. The prefix stays byte-stable; the breakpoint moves forward.
            _mark_cache(msgs[-1])
        thinking = thinking_param(self.model, self._thinking)
        if thinking:
            params["thinking"] = thinking
        if self._effort:
            params["output_config"] = {"effort": self._effort}
        if fallbacks_enabled(self.model, self._fallbacks):
            params["extra_headers"] = {"anthropic-beta": FALLBACK_BETA}
            params["extra_body"] = {"fallbacks": "default"}
        return params

    async def complete(self, messages, tools=None) -> ModelResponse:
        client = self._get_client()
        try:
            message = await client.messages.create(**self._params(messages, tools))
        except Exception as exc:  # noqa: BLE001 — mapped below
            raise _map_error(exc) from exc
        return _to_response(message)

    async def stream(self, messages, tools=None) -> AsyncIterator[StreamChunk]:
        client = self._get_client()
        try:
            async with client.messages.stream(**self._params(messages, tools)) as stream:
                async for event in stream:
                    if getattr(event, "type", None) == "text" and getattr(event, "text", ""):
                        yield StreamChunk(text=event.text)
                message = await stream.get_final_message()
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _map_error(exc) from exc
        yield StreamChunk(final=_to_response(message))

    async def close(self) -> None:
        client = self._client
        if client is not None and hasattr(client, "close"):
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass


def _mark_cache(message: dict[str, Any]) -> None:
    """Attach an ephemeral cache breakpoint to the last block of ``message``."""
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
        return
    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict) and last.get("type") in {"text", "tool_result", "tool_use"}:
            content[-1] = {**last, "cache_control": {"type": "ephemeral"}}


def _to_response(message: Any) -> ModelResponse:
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    raw_blocks: list[dict[str, Any]] = []
    for block in message.content:
        btype = getattr(block, "type", None)
        raw_blocks.append(
            block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else dict(block)
        )
        if btype == "text":
            text_parts.append(block.text)
        elif btype == "tool_use":
            inp = block.input if isinstance(block.input, dict) else {"_malformed_json": block.input}
            calls.append(ToolCall(id=block.id, name=block.name, arguments=inp))
    stop = getattr(message, "stop_reason", None)
    if stop == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise ProviderError(
            "anthropic: the model declined this request (refusal"
            + (f", category {category}" if category else "")
            + ")"
        )
    usage = getattr(message, "usage", None)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0) if usage else 0
    cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0) if usage else 0
    in_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
    return ModelResponse(
        text="".join(text_parts),
        tool_calls=calls,
        usage=TokenUsage(
            input_tokens=in_tokens + cache_read + cache_write,
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0) if usage else 0,
            cached_input_tokens=cache_read,
        ),
        finish_reason={"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length"}.get(
            stop, stop
        ),
        raw_metadata={
            "id": getattr(message, "id", None),
            "model": getattr(message, "model", None),
            "provider_content": raw_blocks,
            "provider_model": getattr(message, "model", None),
            "stop_reason": stop,
        },
    )


def _map_error(exc: Exception) -> ProviderError:
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return ProviderError(str(exc))
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, anthropic.RateLimitError):
        return ProviderRateLimitError(f"anthropic: rate limited: {exc.message}")
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return ProviderAuthenticationError(f"anthropic: {exc.message}")
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderTimeoutError("anthropic: request timed out")
    if isinstance(exc, anthropic.BadRequestError | anthropic.RequestTooLargeError):
        msg = str(getattr(exc, "message", exc))
        if any(
            k in msg.lower()
            for k in ("prompt is too long", "too many tokens", "context", "maximum")
        ):
            return ProviderContextOverflowError(f"anthropic: context overflow: {msg[:200]}")
        return ProviderError(f"anthropic: bad request: {msg[:300]}")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500 or exc.status_code == 529:
            return ProviderUnavailableError(f"anthropic: HTTP {exc.status_code}: {exc.message}")
        return ProviderError(f"anthropic: HTTP {exc.status_code}: {exc.message}")
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderUnavailableError(f"anthropic: connection error: {exc.message}")
    if isinstance(exc, anthropic.APIResponseValidationError):
        return ProviderMalformedResponseError(f"anthropic: malformed response: {exc}")
    return ProviderError(f"anthropic: {exc.__class__.__name__}: {exc}")
