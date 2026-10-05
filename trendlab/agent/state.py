"""Explicit, testable agent state machine (spec §71)."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum


class AgentState(StrEnum):
    IDLE = "IDLE"
    THINKING = "THINKING"
    PLANNING = "PLANNING"
    WAITING_PERMISSION = "WAITING_PERMISSION"
    RUNNING_TOOL = "RUNNING_TOOL"
    WAITING_SUBAGENT = "WAITING_SUBAGENT"
    VALIDATING = "VALIDATING"
    RECOVERING = "RECOVERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


_TERMINAL = {AgentState.COMPLETED, AgentState.FAILED, AgentState.CANCELED}

TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.IDLE: {AgentState.THINKING, AgentState.PLANNING, AgentState.CANCELED},
    AgentState.THINKING: {
        AgentState.PLANNING,
        AgentState.RUNNING_TOOL,
        AgentState.WAITING_PERMISSION,
        AgentState.VALIDATING,
        AgentState.RECOVERING,
        *_TERMINAL,
    },
    AgentState.PLANNING: {AgentState.THINKING, AgentState.RUNNING_TOOL, *_TERMINAL},
    AgentState.WAITING_PERMISSION: {AgentState.RUNNING_TOOL, AgentState.THINKING, *_TERMINAL},
    AgentState.RUNNING_TOOL: {
        AgentState.THINKING,
        AgentState.WAITING_PERMISSION,
        AgentState.VALIDATING,
        AgentState.RECOVERING,
        *_TERMINAL,
    },
    AgentState.WAITING_SUBAGENT: {AgentState.THINKING, *_TERMINAL},
    AgentState.VALIDATING: {AgentState.THINKING, AgentState.RECOVERING, *_TERMINAL},
    AgentState.RECOVERING: {AgentState.THINKING, *_TERMINAL},
    AgentState.COMPLETED: {AgentState.IDLE},
    AgentState.FAILED: {AgentState.IDLE},
    AgentState.CANCELED: {AgentState.IDLE},
}


class InvalidTransition(Exception):
    pass


class AgentStateMachine:
    def __init__(self, on_change: Callable[[AgentState, AgentState], None] | None = None) -> None:
        self.state = AgentState.IDLE
        self._on_change = on_change
        self.history: list[AgentState] = [AgentState.IDLE]

    def transition(self, new: AgentState | str) -> None:
        new = AgentState(new)
        if new == self.state:
            return
        if new not in TRANSITIONS[self.state]:
            raise InvalidTransition(f"{self.state} → {new}")
        old, self.state = self.state, new
        self.history.append(new)
        if self._on_change:
            self._on_change(old, new)

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL
