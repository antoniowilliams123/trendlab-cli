"""Normalized provider contract. Nothing outside ``providers/`` sees vendor JSON."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, Field


class ProviderError(Exception):
    """Base class. ``retryable`` tells the gateway whether backoff makes sense."""

    retryable = False


class ProviderRateLimitError(ProviderError):
    retryable = True


class ProviderTimeoutError(ProviderError):
    retryable = True


class ProviderUnavailableError(ProviderError):
    retryable = True


class ProviderAuthenticationError(ProviderError):
    retryable = False


class ProviderContextOverflowError(ProviderError):
    retryable = False


class ProviderMalformedResponseError(ProviderError):
    retryable = True


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0


class ModelResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: TokenUsage = TokenUsage()
    finish_reason: str | None = None
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class StreamChunk(BaseModel):
    """One incremental piece of a streamed response. ``final`` carries the full response."""

    text: str = ""
    thinking: str = ""  # model reasoning summary, when the provider exposes it
    final: ModelResponse | None = None


class ModelCapabilities(BaseModel):
    native_tools: bool = True
    streaming: bool = True
    structured_output: bool = False
    parallel_tool_calls: bool = True
    context_window: int | None = None
    local: bool = False

    @property
    def privacy_label(self) -> str:
        return "LOCAL" if self.local else "REMOTE"


class ModelProvider(ABC):
    name: str = "base"
    model: str = ""

    @abstractmethod
    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> ModelResponse: ...

    async def stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[StreamChunk]:
        """Default: emulate streaming with one chunk. Providers override for real SSE."""
        response = await self.complete(messages, tools)
        if response.text:
            yield StreamChunk(text=response.text)
        yield StreamChunk(final=response)

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities()

    @property
    def ref(self) -> str:
        return f"{self.name}:{self.model}"

    async def close(self) -> None:  # noqa: B027
        """Release resources."""
