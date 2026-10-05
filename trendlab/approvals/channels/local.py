"""Local terminal approval channel.

Renders the request with Rich and reads a one-key decision. When remote
approval is enabled the prompt says so and the two channels race: the first
valid decision wins and the other is withdrawn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from trendlab.approvals.channels.base import ApprovalChannel, ChannelError
from trendlab.approvals.models import (
    ApprovalDecision,
    ApprovalError,
    ApprovalRequest,
    ApprovalScope,
    ApprovalStatus,
    DecisionResult,
)
from trendlab.ui.console import ConsoleInput

if TYPE_CHECKING:
    from trendlab.approvals.manager import ApprovalManager

CommandHandler = Callable[[str], Awaitable[None]]

_RISK_STYLE = {"low": "green", "medium": "yellow", "high": "bold red"}


class LocalTerminalChannel(ApprovalChannel):
    name = "local"
    remote = False

    def __init__(
        self,
        console: Console,
        console_input: ConsoleInput,
        command_handler: CommandHandler | None = None,
    ) -> None:
        self.console = console
        self.input = console_input
        self.command_handler = command_handler
        self._manager: ApprovalManager | None = None
        self._readers: dict[str, asyncio.Task] = {}

    async def start(self, manager: ApprovalManager) -> None:
        self._manager = manager

    async def stop(self) -> None:
        for task in list(self._readers.values()):
            task.cancel()
        self._readers.clear()

    async def dispatch(self, request: ApprovalRequest) -> None:
        self.render(request)
        if not self.input.interactive:
            raise ChannelError("no interactive terminal attached")
        self._readers[request.approval_id] = asyncio.create_task(self._read_decision(request))

    def render(self, request: ApprovalRequest) -> None:
        assert self._manager is not None
        if request.kind == "question":
            body = request.explanation
            if request.options:
                body += "\n" + "  ".join(f"[{i + 1}] {o}" for i, o in enumerate(request.options))
            self.console.print(Panel(body, title="❓ TrendLab asks", border_style="cyan"))
            hint = "  type your answer (or an option number) and press Enter"
            if self._manager.remote_enabled:
                hint += "  ·  also answerable on your phone"
            self.console.print(hint)
            return
        table = Table.grid(padding=(0, 1))
        table.add_row("[bold]Action:[/bold]", request.summary)
        if request.command:
            table.add_row("[bold]Command:[/bold]", f"[cyan]{request.command}[/cyan]")
        if request.affected_files:
            table.add_row("[bold]Files:[/bold]", ", ".join(request.affected_files[:5]))
        table.add_row("[bold]Directory:[/bold]", request.cwd)
        style = _RISK_STYLE.get(request.risk.value, "white")
        table.add_row("[bold]Risk:[/bold]", f"[{style}]{request.risk.value.upper()}[/{style}]")
        if request.explanation:
            table.add_row("[bold]Agent says:[/bold]", f"[dim]{request.explanation}[/dim]")
        table.add_row(
            "[bold]Expires:[/bold]", f"in {max(1, request.seconds_remaining() // 60)} min"
        )
        self.console.print(Panel(table, title="⏳ Waiting for approval", border_style="yellow"))
        if self._manager.remote_enabled and request.remote_allowed:
            self.console.print(
                "[dim]Remote approval request sent — decide here or on your phone.[/dim]"
            )
        elif self._manager.remote_enabled and not request.remote_allowed:
            self.console.print("[dim]High-risk operation: local approval required.[/dim]")
        keys = "[bold]y[/bold] approve once"
        if request.session_scope_allowed:
            keys += (
                "  ·  [bold]s[/bold] approve for session  ·  [bold]p[/bold] always for this project"
            )
        keys += "  ·  [bold]n[/bold] deny"
        self.console.print(f"  {keys}  [dim](id {request.approval_id[:8]})[/dim]")

    async def _read_decision(self, request: ApprovalRequest) -> None:
        assert self._manager is not None
        while True:
            line = await self.input.readline()
            if line is None:
                return
            text = line.strip()
            if text.startswith("/") and self.command_handler is not None:
                await self.command_handler(text)
                continue
            if request.kind == "question":
                if not text:
                    continue
                if text.isdigit() and 1 <= int(text) <= len(request.options):
                    text = request.options[int(text) - 1]
                try:
                    self._manager.answer(
                        request.approval_id, text, via=self.name, by="terminal", trusted=True
                    )
                except ApprovalError as exc:
                    self.console.print(f"[yellow]Answer not applied: {exc}[/yellow]")
                return
            lowered = text.lower()
            if lowered in {"y", "yes"}:
                decision, scope = ApprovalDecision.APPROVE, ApprovalScope.ONCE
            elif lowered in {"s", "session"} and request.session_scope_allowed:
                decision, scope = ApprovalDecision.APPROVE, ApprovalScope.SESSION
            elif lowered in {"p", "project", "a", "always"} and request.session_scope_allowed:
                decision, scope = ApprovalDecision.APPROVE, ApprovalScope.PROJECT
            elif lowered in {"n", "no", "deny"}:
                decision, scope = ApprovalDecision.DENY, ApprovalScope.ONCE
            else:
                self.console.print(
                    "[dim]Type y (once), s (session), p (project) or n (deny).[/dim]"
                )
                continue
            try:
                self._manager.decide(
                    request.approval_id, decision, scope, via=self.name, by="terminal", trusted=True
                )
            except ApprovalError as exc:
                self.console.print(f"[yellow]Decision not applied: {exc}[/yellow]")
            return

    async def withdraw(self, request: ApprovalRequest, result: DecisionResult) -> None:
        task = self._readers.pop(request.approval_id, None)
        if task is not None and not task.done():
            task.cancel()
        where = "remotely" if result.via and result.via != self.name else "locally"
        if request.kind == "question":
            if result.status == ApprovalStatus.APPROVED:
                self.console.print(f"[cyan]Answered {where}:[/cyan] {result.reason}")
            else:
                self.console.print(f"[yellow]Question {result.status.value}[/yellow]")
            return
        if result.status == ApprovalStatus.APPROVED:
            scope = {
                ApprovalScope.SESSION: " for this session",
                ApprovalScope.PROJECT: " for this project",
            }.get(result.scope, "")
            self.console.print(
                f"[green]✓ Approved {where}{scope}[/green]\n[dim]Continuing...[/dim]"
            )
        elif result.status == ApprovalStatus.DENIED:
            self.console.print(f"[red]✗ Denied {where}[/red]")
        elif result.status == ApprovalStatus.EXPIRED:
            self.console.print("[yellow]⌛ Approval expired[/yellow]")
        elif result.status == ApprovalStatus.CANCELED:
            self.console.print(f"[dim]Approval canceled ({result.reason})[/dim]")
        elif result.status == ApprovalStatus.SUPERSEDED:
            self.console.print("[dim]Approval superseded by a newer request[/dim]")
