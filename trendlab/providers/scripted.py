"""Deterministic provider for tests and offline demos."""

from __future__ import annotations

from typing import Any

from trendlab.providers.base import ModelProvider, ModelResponse


class ScriptedProvider(ModelProvider):
    name = "scripted"
    model = "scripted"

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def complete(self, messages, tools=None) -> ModelResponse:
        self.calls.append(list(messages))
        if not self._responses:
            return ModelResponse(text="(script exhausted)", finish_reason="stop")
        return self._responses.pop(0)
