"""Deterministic provider for tests and offline demos."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from trendlab.providers.base import ModelCapabilities, ModelProvider, ModelResponse


class ScriptedProvider(ModelProvider):
    name = "scripted"
    model = "scripted"

    def __init__(
        self,
        responses: list[
            ModelResponse | Exception | Callable[[list[dict[str, Any]]], ModelResponse]
        ],
        *,
        local: bool = False,
        native_tools: bool = True,
    ) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []
        self._caps = ModelCapabilities(local=local, native_tools=native_tools)

    async def complete(self, messages, tools=None) -> ModelResponse:
        self.calls.append(list(messages))
        if not self._responses:
            return ModelResponse(text="(script exhausted)", finish_reason="stop")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(messages)
        return item

    def capabilities(self) -> ModelCapabilities:
        return self._caps
