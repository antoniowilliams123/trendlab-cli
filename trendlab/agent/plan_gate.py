"""Plan-approval gate (spec §90): the first change of a run waits for a human "go".

When enabled, the moment the agent is about to make its first mutation in a run, the current
plan (or, with no explicit plan, the files about to change) is sent through every approval
channel — terminal, phone web page, Telegram buttons. Approve and the run proceeds normally;
deny and every mutation in that run is refused, so you can steer the agent before it edits.

The gate is independent of the permission mode: it is opt-in and asks exactly once per run.
"""

from __future__ import annotations

import itertools
from datetime import timedelta
from pathlib import Path
from typing import Any

from trendlab.agent.tasks import Plan
from trendlab.approvals.manager import ApprovalManager
from trendlab.approvals.models import ApprovalStatus
from trendlab.config.schema import PlanGateConfig
from trendlab.permissions.engine import PermissionRequest
from trendlab.permissions.models import OperationCategory
from trendlab.telemetry.events import EventBus, EventType

_counter = itertools.count(1)


class PlanRejected(Exception):
    """The human declined the plan; no mutation may run in this turn."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PlanGate:
    def __init__(
        self,
        config: PlanGateConfig,
        approvals: ApprovalManager,
        plan: Plan,
        events: EventBus,
        project_root: Path,
    ) -> None:
        self.config = config
        self.approvals = approvals
        self.plan = plan
        self.events = events
        self.project_root = project_root
        self.approved = False
        self.rejected: str | None = None

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def reset(self) -> None:
        """Called at the start of every run."""
        self.approved = False
        self.rejected = None

    def _preview(self, files: list[str]) -> str:
        if self.plan.tasks:
            return self.plan.render()
        shown = ", ".join(files[:8]) + (" …" if len(files) > 8 else "")
        return f"(no explicit plan) first change touches: {shown}"

    async def check(self, files: list[str]) -> None:
        """Block until the plan is approved; raise PlanRejected when it is not."""
        if not self.enabled or self.approved:
            return
        if self.rejected is not None:
            raise PlanRejected(self.rejected)
        n = next(_counter)
        perm = PermissionRequest(
            tool="plan",
            category=OperationCategory.PROJECT_WRITE,
            summary="Approve the plan before the first change",
            cwd=str(self.project_root),
            affected_files=list(files),
            explanation="The agent is about to start editing. Review the plan and approve or deny.",
            preview=self._preview(files),
            args={"plan": self.plan.to_json(), "files": list(files), "n": n},
        )
        self.events.emit(
            EventType.PLAN_GATE_REQUESTED,
            session_id=self.approvals.session_id,
            tasks=len(self.plan.tasks),
            files=list(files),
        )
        result = await self.approvals.request(
            perm,
            timeout=timedelta(minutes=self.config.timeout_minutes),
            can_persist=False,
        )
        if result.status == ApprovalStatus.APPROVED:
            self.approved = True
            self.events.emit(
                EventType.PLAN_APPROVED,
                session_id=self.approvals.session_id,
                via=result.via,
                by=result.by,
            )
            return
        reason = result.reason or result.status.value
        self.rejected = reason
        self.events.emit(
            EventType.PLAN_REJECTED,
            session_id=self.approvals.session_id,
            status=result.status.value,
            reason=reason,
        )
        raise PlanRejected(reason)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "approved": self.approved,
            "rejected": self.rejected,
            "timeout_minutes": self.config.timeout_minutes,
        }
