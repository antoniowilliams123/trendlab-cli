"""Event bus for TrendLab CLI.

The runtime emits structured events; the UI, the audit log and tests subscribe.
Nothing in the runtime depends on who is listening, which is what lets remote
approval, the terminal UI and future interfaces coexist.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from trendlab.security.redaction import redact_structure


class EventType(StrEnum):
    # Agent / runtime
    AGENT_STATE_CHANGED = "agent.state_changed"
    MODEL_CALL_STARTED = "model.call_started"
    MODEL_TOKEN = "model.token"
    MODEL_CALL_COMPLETED = "model.call_completed"
    PROVIDER_RETRY = "provider.retry"
    PROVIDER_FALLBACK = "provider.fallback"
    COST_UPDATED = "cost.updated"
    BUDGET_WARNING = "budget.warning"
    BUDGET_EXCEEDED = "budget.exceeded"
    TASK_UPDATED = "task.updated"
    PLAN_UPDATED = "plan.updated"
    PLAN_GATE_REQUESTED = "plan.gate_requested"
    PLAN_APPROVED = "plan.approved"
    PLAN_REJECTED = "plan.rejected"
    SESSION_BRANCHED = "session.branched"
    AGENT_SPAWNED = "agent.spawned"
    AGENT_COMPLETED = "agent.completed"
    CONTEXT_COMPACTED = "context.compacted"
    CONTEXT_BUDGET = "context.budget"
    SESSION_CHECKPOINTED = "session.checkpointed"
    SESSION_RESTORED = "session.restored"
    LOOP_DETECTED = "loop.detected"
    RECOVERY = "recovery.action"
    SECRET_WRITE_BLOCKED = "security.secret_write_blocked"
    DIAGNOSTICS = "tool.diagnostics"
    TOOLS_PARALLEL = "tool.parallel"
    FILES_ATTACHED = "prompt.files_attached"
    PATHS_TRANSLATED = "prompt.paths_translated"  # Windows → WSL paths rewritten
    CUSTOM_COMMAND = "prompt.custom_command"
    SANDBOX_STATUS = "security.sandbox"
    HOOK_RUN = "hook.run"
    HOOK_BLOCKED = "hook.blocked"
    QUESTION_ASKED = "question.asked"
    QUESTION_ANSWERED = "question.answered"
    STEERED = "run.steered"
    IMAGES_ATTACHED = "run.images_attached"
    VISION_AUTOSWITCH = "model.vision_autoswitch"  # image prompt routed to a vision model
    MEMORY_UPDATED = "memory.updated"  # project memory gained facts
    PROJECT_ROOT_CHANGED = "session.project_root_changed"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELED = "run.canceled"
    NOTIFICATION_SENT = "notification.sent"
    NOTIFICATION_FAILED = "notification.failed"
    MCP_SERVER_STARTED = "mcp.server_started"
    MCP_SERVER_FAILED = "mcp.server_failed"
    TOOL_REQUESTED = "tool.requested"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_SKIPPED = "tool.skipped"  # denied / invalid / unknown / blocked: never ran
    TOOL_OUTPUT = "tool.output"  # live tail of a running command (transient: not persisted)
    FILE_CHANGED = "file.changed"
    # Permissions
    PERMISSION_REQUESTED = "permission.requested"
    PERMISSION_DECIDED = "permission.decided"
    # Approvals (remote-capable)
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DISPATCHED = "approval.dispatched"
    APPROVAL_DELIVERY_FAILED = "approval.delivery_failed"
    APPROVAL_RECEIVED = "approval.received"
    APPROVAL_APPROVED = "approval.approved"
    APPROVAL_DENIED = "approval.denied"
    APPROVAL_EXPIRED = "approval.expired"
    APPROVAL_CANCELED = "approval.canceled"
    APPROVAL_SUPERSEDED = "approval.superseded"
    APPROVAL_REJECTED = "approval.rejected"  # invalid/replayed/mismatched decision attempt
    # Remote channel lifecycle
    REMOTE_CHANNEL_STARTED = "remote.channel_started"
    REMOTE_CHANNEL_STOPPED = "remote.channel_stopped"
    REMOTE_AUTH_FAILED = "remote.auth_failed"
    REMOTE_MESSAGE = "remote.message"  # inbound text from a remote-control surface
    REMOTE_DELIVERY_FAILED = "remote.delivery_failed"
    # Session
    SESSION_STARTED = "session.started"
    SESSION_RESUMED = "session.resumed"


class Event(BaseModel):
    type: EventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    session_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        rec = {
            "event": self.type.value,
            "ts": self.timestamp.isoformat(),
            "session_id": self.session_id,
        }
        rec.update(redact_structure(self.data))
        return rec


Subscriber = Callable[[Event], None]


class EventBus:
    """Synchronous, thread-safe publish/subscribe bus."""

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._lock = threading.Lock()

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subscribers:
                    self._subscribers.remove(fn)

        return unsubscribe

    def emit(
        self,
        type_: EventType,
        session_id: str | None = None,
        **data: Any,
    ) -> Event:
        event = Event(type=type_, session_id=session_id, data=data)
        with self._lock:
            subscribers = list(self._subscribers)
        for fn in subscribers:
            try:
                fn(event)
            except Exception:  # noqa: BLE001 — a bad subscriber must not break the runtime
                pass
        return event


#: High-frequency progress events that UIs consume live and no sink should store.
TRANSIENT_EVENTS = {"tool.output", "model.token"}


class JsonlEventSink:
    """Appends every event as one redacted JSON line — the audit log."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def __call__(self, event: Event) -> None:
        if event.type.value in TRANSIENT_EVENTS:
            return
        line = json.dumps(event.to_record(), default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class EventRecorder:
    """In-memory subscriber used by tests and the ``/approvals`` command."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def of_type(self, type_: EventType) -> list[Event]:
        return [e for e in self.events if e.type == type_]
