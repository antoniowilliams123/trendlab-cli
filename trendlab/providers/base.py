"""Normalized provider contract. Nothing outside ``providers/`` sees vendor JSON."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class ProviderError(Exception):
    pass


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class ModelResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: TokenUsage = TokenUsage()
    finish_reason: str | None = None


class ModelProvider(ABC):
    name: str = "base"
    model: str = ""

    @abstractmethod
    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> ModelResponse: ...

    async def close(self) -> None:  # noqa: B027
        """Release resources."""
