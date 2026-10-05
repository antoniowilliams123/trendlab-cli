"""Approval channel for the Textual TUI: shows a modal, forwards the decision to the manager."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from trendlab.approvals.channels.base import ApprovalChannel
from trendlab.approvals.models import ApprovalRequest, DecisionResult

if TYPE_CHECKING:
    from trendlab.approvals.manager import ApprovalManager

Presenter = Callable[[ApprovalRequest], None]
Withdrawer = Callable[[ApprovalRequest, DecisionResult], None]


class TextualChannel(ApprovalChannel):
    name = "local"  # trusted, same as the terminal prompt
    remote = False

    def __init__(self, present: Presenter, withdraw: Withdrawer) -> None:
        self._present = present
        self._withdraw = withdraw
        self._manager: ApprovalManager | None = None

    async def start(self, manager: ApprovalManager) -> None:
        self._manager = manager

    async def dispatch(self, request: ApprovalRequest) -> None:
        self._present(request)

    async def withdraw(self, request: ApprovalRequest, result: DecisionResult) -> None:
        self._withdraw(request, result)
