"""Notification providers tell the user *that* attention is required.

They carry no authority: a notification contains a redacted summary and a link
to the approval UI, never a token or a one-tap approve action. Authorization
happens only through an authenticated ApprovalChannel.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel

from trendlab import PRODUCT_NAME
from trendlab.approvals.models import ApprovalRequest
from trendlab.security.redaction import redact_text


class NotificationError(Exception):
    """Delivery failed. The approval stays pending; the runtime emits approval.delivery_failed."""


class Notification(BaseModel):
    title: str
    body: str
    url: str | None = None
    approval_id: str
    risk: str
    priority: str = "default"  # "default" | "high"


class NotificationProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def send(self, notification: Notification) -> None: ...

    async def close(self) -> None:  # noqa: B027 — optional
        """Release network resources."""


def build_notification(
    request: ApprovalRequest, approval_url: str | None, project: str
) -> Notification:
    if request.kind == "question":
        q_lines = [
            f"Machine: {request.machine}",
            f"Project: {project}",
            f"Question: {request.explanation}",
        ]
        if request.options:
            q_lines.append("Options: " + " / ".join(request.options))
        q_lines.append(f"Expires in: {max(1, request.seconds_remaining() // 60)} min")
        if approval_url:
            q_lines.append(f"Answer: {approval_url}")
        return Notification(
            title=f"{PRODUCT_NAME} — Question for you",
            body=redact_text("\n".join(q_lines)),
            url=approval_url,
            approval_id=request.approval_id,
            risk="low",
        )
    lines = [
        f"Machine: {request.machine}",
        f"Project: {project}",
        f"Action: {request.summary}",
    ]
    if request.command:
        lines.append(f"Command: {request.command}")
    if request.affected_files:
        shown = ", ".join(request.affected_files[:5])
        if len(request.affected_files) > 5:
            shown += f" (+{len(request.affected_files) - 5} more)"
        lines.append(f"Files: {shown}")
    if request.preview and request.preview.startswith("---"):
        added = sum(
            1
            for ln in request.preview.splitlines()
            if ln.startswith("+") and not ln.startswith("+++")
        )
        removed = sum(
            1
            for ln in request.preview.splitlines()
            if ln.startswith("-") and not ln.startswith("---")
        )
        lines.append(f"Change: +{added} -{removed} lines (diff on the approval page)")
    lines.append(f"Risk: {request.risk.value.capitalize()}")
    lines.append(f"Expires in: {max(1, request.seconds_remaining() // 60)} min")
    if not request.remote_allowed:
        lines.append("This operation must be approved at the terminal.")
    elif approval_url:
        lines.append(f"Open: {approval_url}")
    body = redact_text("\n".join(lines))
    return Notification(
        title=f"{PRODUCT_NAME} — Approval Required",
        body=body,
        url=approval_url if request.remote_allowed else None,
        approval_id=request.approval_id,
        risk=request.risk.value,
        priority="high" if request.risk.value == "high" else "default",
    )
