"""The agent loop: model → tool calls → tool runtime → observations → model …

Permission waits happen *inside* a tool call; the loop simply awaits the tool
runtime, so a remote approval pauses only that operation while session and
conversation state stay intact.
"""

from __future__ import annotations

import json
from typing import Any

from trendlab.agent.state import AgentState, AgentStateMachine
from trendlab.providers.base import ModelProvider, ModelResponse, ProviderError
from trendlab.telemetry.events import EventBus, EventType
from trendlab.tools.runtime import ToolRuntime


class AgentRuntime:
    def __init__(
        self,
        *,
        provider: ModelProvider,
        tools: ToolRuntime,
        events: EventBus,
        session_id: str,
        system_prompt: str,
        max_iterations: int = 50,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.events = events
        self.session_id = session_id
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        self.state = AgentStateMachine(on_change=self._emit_state)
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.tools._set_state = self._tool_state_hook  # noqa: SLF001 — wiring

    def _emit_state(self, old: AgentState, new: AgentState) -> None:
        self.events.emit(
            EventType.AGENT_STATE_CHANGED, session_id=self.session_id, old=old.value, new=new.value
        )

    def _tool_state_hook(self, name: str) -> None:
        try:
            self.state.transition(AgentState(name))
        except Exception:  # noqa: BLE001 — hooks never break execution
            pass

    def set_provider(self, provider: ModelProvider) -> None:
        """Hot model switch: conversation, session and tool state are preserved."""
        self.provider = provider

    async def run(self, prompt: str) -> str:
        if self.state.terminal:
            self.state.transition(AgentState.IDLE)
        self.messages.append({"role": "user", "content": prompt})
        try:
            for _ in range(self.max_iterations):
                self.state.transition(AgentState.THINKING)
                response = await self._complete()
                if response.tool_calls:
                    self.messages.append(_assistant_message(response))
                    for call in response.tool_calls:
                        self.state.transition(AgentState.RUNNING_TOOL)
                        result = await self.tools.execute(call)
                        self.messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "name": call.name,
                                "content": result.output,
                            }
                        )
                    continue
                self.messages.append({"role": "assistant", "content": response.text})
                self.state.transition(AgentState.COMPLETED)
                return response.text
            self.state.transition(AgentState.FAILED)
            return f"Stopped after {self.max_iterations} iterations without completing."
        except ProviderError as exc:
            self.state.transition(AgentState.FAILED)
            return f"Provider error: {exc}"

    async def _complete(self) -> ModelResponse:
        self.events.emit(
            EventType.MODEL_CALL_STARTED,
            session_id=self.session_id,
            provider=self.provider.name,
            model=self.provider.model,
        )
        response = await self.provider.complete(self.messages, self.tools.registry.schemas())
        self.total_input_tokens += response.usage.input_tokens
        self.total_output_tokens += response.usage.output_tokens
        self.events.emit(
            EventType.MODEL_CALL_COMPLETED,
            session_id=self.session_id,
            provider=self.provider.name,
            model=self.provider.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            tool_calls=len(response.tool_calls),
        )
        return response


def _assistant_message(response: ModelResponse) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": response.text or None,
        "tool_calls": [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
            }
            for c in response.tool_calls
        ],
    }
