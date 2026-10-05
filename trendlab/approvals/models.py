"""Approval request/decision models.

An ``ApprovalRequest`` is the one object every channel sees. It is built from a
``PermissionRequest`` after the permission engine said ASK, is redacted at
construction time, and carries a fingerprint of the exact operation so a
decision can never be applied to a different operation.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from trendlab.permissions.engine import PermissionRequest
from trendlab.permissions.models import OperationCategory, RiskLevel
from trendlab.security.redaction import redact_text


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELED = "canceled"
    SUPERSEDED = "superseded"


class ApprovalScope(StrEnum):
    ONCE = "once"
    SESSION = "session"
    PROJECT = "project"  # persisted in .trendlab/permissions.toml; local channels only


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    DENY = "deny"


class ApprovalError(Exception):
    """A decision could not be applied. ``code`` is stable for API responses/tests."""

    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def _now() -> datetime:
    return datetime.now(UTC)


class ApprovalRequest(BaseModel):
    approval_id: str
    session_id: str
    task_id: str | None = None
    created_at: datetime
    expires_at: datetime
    tool: str
    category: OperationCategory
    risk: RiskLevel
    summary: str
    command: str | None = None
    cwd: str
    affected_files: list[str] = Field(default_factory=list)
    explanation: str = ""
    preview: str | None = None  # redacted unified diff or operation preview
    requested_scope: ApprovalScope = ApprovalScope.ONCE
    machine: str
    fingerprint: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    # Which remote channels this request may be decided through. High-risk
    # operations are local-only unless configured otherwise.
    remote_allowed: bool = True
    session_scope_allowed: bool = True
    # Per-request secret shared only with authenticated clients; required on decisions.
    decision_token: str = Field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)

    @classmethod
    def from_permission_request(
        cls,
        req: PermissionRequest,
        *,
        session_id: str,
        machine: str,
        timeout: timedelta,
        remote_allowed: bool,
        session_scope_allowed: bool,
    ) -> ApprovalRequest:
        now = _now()
        return cls(
            approval_id=secrets.token_urlsafe(16),
            session_id=session_id,
            task_id=req.task_id,
            created_at=now,
            expires_at=now + timeout,
            tool=req.tool,
            category=req.category,
            risk=req.risk,
            summary=redact_text(req.summary),
            command=redact_text(req.command) if req.command else None,
            cwd=req.cwd,
            affected_files=[redact_text(p) for p in req.affected_files],
            explanation=redact_text(req.explanation),
            preview=redact_text(req.preview)[:20_000] if req.preview else None,
            machine=machine,
            fingerprint=req.fingerprint,
            remote_allowed=remote_allowed,
            session_scope_allowed=session_scope_allowed,
        )

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or _now()) >= self.expires_at

    def seconds_remaining(self, now: datetime | None = None) -> int:
        return max(0, int((self.expires_at - (now or _now())).total_seconds()))

    def public_view(self, include_decision_token: bool = False) -> dict[str, Any]:
        """What a channel may show. Never includes env/secrets; token only for auth'd clients."""
        view = {
            "approval_id": self.approval_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "seconds_remaining": self.seconds_remaining(),
            "tool": self.tool,
            "category": self.category.value,
            "risk": self.risk.value,
            "summary": self.summary,
            "command": self.command,
            "cwd": self.cwd,
            "affected_files": list(self.affected_files),
            "explanation": self.explanation,
            "preview": self.preview,
            "requested_scope": self.requested_scope.value,
            "machine": self.machine,
            "fingerprint": self.fingerprint,
            "status": self.status.value,
            "remote_allowed": self.remote_allowed,
            "session_scope_allowed": self.session_scope_allowed,
        }
        if include_decision_token:
            view["decision_token"] = self.decision_token
        return view

    def to_row(self, process_id: str) -> dict[str, Any]:
        return {
            "id": self.approval_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "status": self.status.value,
            "tool": self.tool,
            "category": self.category.value,
            "risk": self.risk.value,
            "summary": self.summary,
            "command": self.command,
            "cwd": self.cwd,
            "affected_files": list(self.affected_files),
            "explanation": self.explanation,
            "preview": self.preview,
            "requested_scope": self.requested_scope.value,
            "machine": self.machine,
            "fingerprint": self.fingerprint,
            "process_id": process_id,
        }


class DecisionResult(BaseModel):
    """Recorded outcome of an approval request."""

    approval_id: str
    status: ApprovalStatus
    decision: ApprovalDecision | None = None
    scope: ApprovalScope | None = None
    decided_at: datetime | None = None
    via: str | None = None  # channel name: "local", "web", "cli", ...
    by: str | None = None  # client identifier (e.g. remote address), never credentials
    reason: str | None = None

    @property
    def approved(self) -> bool:
        return self.status == ApprovalStatus.APPROVED
