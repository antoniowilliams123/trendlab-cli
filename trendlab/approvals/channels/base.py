"""ApprovalChannel — the surface through which a human decides a pending approval.

A channel *presents* requests and *forwards* decisions to the ApprovalManager.
It never decides anything itself and never executes anything. The manager is
the only component that validates and records decisions, so the permission
system does not care whether the decision came from the terminal, a phone, or
a future Slack bot.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from trendlab.approvals.models import ApprovalRequest, DecisionResult

if TYPE_CHECKING:
    from trendlab.approvals.manager import ApprovalManager


class ChannelError(Exception):
    """Raised by a channel when it cannot present a request (server down, no TTY...)."""


class ApprovalChannel(ABC):
    #: Stable channel name recorded in the audit trail ("local", "web", ...).
    name: str = "base"
    #: Remote channels are subject to the stricter remote rules (tokens, risk gating).
    remote: bool = False

    async def start(self, manager: ApprovalManager) -> None:  # noqa: B027 — optional hook
        """Attach to the manager (open sockets, spawn readers...)."""

    async def stop(self) -> None:  # noqa: B027 — optional hook
        """Release resources."""

    @abstractmethod
    async def dispatch(self, request: ApprovalRequest) -> None:
        """Make the request visible to the human. Raise ChannelError if impossible."""

    async def withdraw(self, request: ApprovalRequest, result: DecisionResult) -> None:  # noqa: B027
        """The request is no longer pending (decided elsewhere, expired, canceled)."""
