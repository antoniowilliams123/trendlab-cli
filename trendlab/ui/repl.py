"""Rich-based interactive REPL (the pre-Textual terminal UI)."""

from __future__ import annotations

import shlex

from rich.console import Console
from rich.table import Table

from trendlab import PRODUCT_NAME, __version__
from trendlab.app import TrendLabApp
from trendlab.approvals.channels.base import ChannelError
from trendlab.approvals.models import ApprovalDecision, ApprovalError, ApprovalScope
from trendlab.config.loader import ConfigError
from trendlab.config.schema import PermissionMode
from trendlab.providers.base import ProviderError
from trendlab.providers.registry import create_provider

HELP = """\
/help                     Show this help
/status                   Model, project, mode, session, remote status
/mode <plan|ask|auto_edit|trusted>   Change permission mode
/permissions              Show the policy table for the current mode
/remote [status]          Remote approval status
/remote enable|disable    Start/stop the phone approval server (persists to config)
/remote url               Print the one-time pairing link for your phone
/approvals                List pending approvals
/approvals approve <id> [session] | deny <id>   Decide a pending approval from here
/approvals history        Recent decisions for this session (channel, time, outcome)
/model <provider:model>   Hot-switch the model
/clear                    Clear the screen
/quit                     Exit
"""


class Repl:
    def __init__(self, app: TrendLabApp, console: Console | None = None) -> None:
        self.app = app
        self.console = console or app.console
        self._running = True

    # -- entry point ------------------------------------------------------------------
    async def run(self) -> None:
        await self.app.start(interactive=True, command_handler=self.handle_command)
        self._header()
        try:
            while self._running:
                self.console.print("[bold cyan]>[/bold cyan] ", end="")
                line = await self.app.console_input.readline()
                if line is None:
                    break
                text = line.strip()
                if not text:
                    continue
                if text.startswith("/"):
                    await self.handle_command(text)
                else:
                    await self._prompt(text)
        finally:
            await self.app.stop()

    def _header(self) -> None:
        status = self.app.remote_status()
        remote = f"remote: {'ON ' + str(status['url']) if status['enabled'] else 'off'}"
        self.console.print(
            f"[bold]{PRODUCT_NAME}[/bold] v{__version__} · {self.app.model_ref} · "
            f"{self.app.project_root} · mode {self.app.engine.mode.value} · {remote}"
        )
        self.console.print("[dim]Type a task, or /help for commands.[/dim]")

    async def _prompt(self, text: str) -> None:
        assert self.app.agent is not None
        with self.console.status("[dim]thinking…[/dim]", spinner="dots"):
            reply = await self.app.agent.run(text)
        self.console.print(reply)

    # -- slash commands ------------------------------------------------------------------
    async def handle_command(self, text: str) -> None:
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = text.split()
        cmd, args = parts[0].lower(), parts[1:]
        handler = {
            "/help": self._help,
            "/status": self._status,
            "/mode": self._mode,
            "/permissions": self._permissions,
            "/remote": self._remote,
            "/approvals": self._approvals,
            "/model": self._model,
            "/clear": self._clear,
            "/quit": self._quit,
            "/exit": self._quit,
        }.get(cmd)
        if handler is None:
            self.console.print(f"[red]unknown command {cmd}[/red] — try /help")
            return
        await handler(args)

    async def _help(self, args: list[str]) -> None:
        self.console.print(HELP)

    async def _status(self, args: list[str]) -> None:
        s = self.app.remote_status()
        agent = self.app.agent
        t = Table.grid(padding=(0, 2))
        t.add_row("Model", self.app.model_ref)
        t.add_row("Project", str(self.app.project_root))
        t.add_row("Session", self.app.session_id)
        t.add_row("Mode", self.app.engine.mode.value)
        t.add_row("State", agent.state.state.value if agent else "-")
        t.add_row(
            "Tokens",
            f"{agent.total_input_tokens} in / {agent.total_output_tokens} out" if agent else "-",
        )
        t.add_row("Remote", f"{'enabled ' + str(s['url']) if s['enabled'] else 'disabled'}")
        t.add_row("Notifications", s["notifications"])
        t.add_row("Pending approvals", str(s["pending"]))
        self.console.print(t)

    async def _mode(self, args: list[str]) -> None:
        if not args:
            self.console.print(f"mode: {self.app.engine.mode.value}")
            return
        try:
            self.app.engine.mode = PermissionMode(args[0])
        except ValueError:
            self.console.print("[red]modes: plan, ask, auto_edit, trusted[/red]")
            return
        self.console.print(f"mode set to [bold]{self.app.engine.mode.value}[/bold]")

    async def _permissions(self, args: list[str]) -> None:
        t = Table(title=f"Permissions — mode {self.app.engine.mode.value}")
        t.add_column("Operation")
        t.add_column("Policy")
        for cat, dec in self.app.engine.policy_table().items():
            color = {"allow": "green", "ask": "yellow", "deny": "red"}[dec.value]
            t.add_row(cat.value, f"[{color}]{dec.value.upper()}[/{color}]")
        rules = self.app.engine.session_rules()
        self.console.print(t)
        if rules:
            self.console.print(
                "Session rules: " + ", ".join(f"{k}={v.value}" for k, v in rules.items())
            )

    async def _remote(self, args: list[str]) -> None:
        sub = args[0].lower() if args else "status"
        if sub == "status":
            s = self.app.remote_status()
            t = Table.grid(padding=(0, 2))
            t.add_row(
                "Remote approval",
                "[green]enabled[/green]" if s["enabled"] else "[dim]disabled[/dim]",
            )
            t.add_row("URL", str(s["url"] or "-"))
            t.add_row("Bind", s["bind"] + (" (TLS)" if s["tls"] else ""))
            t.add_row("Request timeout", f"{s['timeout_minutes']} min")
            t.add_row("High-risk via phone", "yes" if s["allow_high_risk"] else "no (local only)")
            t.add_row("Session scope via phone", "yes" if s["allow_session_scope"] else "no")
            t.add_row("Notifications", s["notifications"])
            t.add_row("Pending", str(s["pending"]))
            self.console.print(t)
        elif sub == "enable":
            try:
                url = await self.app.enable_remote()
            except (ChannelError, ConfigError) as exc:
                self.console.print(f"[red]cannot enable remote approval: {exc}[/red]")
                return
            self.console.print(f"[green]Remote approval enabled[/green] at {url}")
            self.console.print(f"Pair your phone once: [bold]{self.app.pairing_url()}[/bold]")
        elif sub == "disable":
            await self.app.disable_remote()
            self.console.print("[yellow]Remote approval disabled[/yellow]")
        elif sub == "url":
            url = self.app.pairing_url()
            if url is None:
                self.console.print(
                    "[red]remote approval is not enabled — /remote enable first[/red]"
                )
            else:
                self.console.print(f"Open this on your phone (keep it private): [bold]{url}[/bold]")
        else:
            self.console.print("[red]usage: /remote [status|enable|disable|url][/red]")

    async def _approvals(self, args: list[str]) -> None:
        mgr = self.app.approvals
        assert mgr is not None
        sub = args[0].lower() if args else "list"
        if sub == "list":
            pending = mgr.pending()
            if not pending:
                self.console.print("[dim]no pending approvals[/dim]")
                return
            t = Table(title="Pending approvals")
            for col in ("id", "risk", "action", "command", "expires in", "remote"):
                t.add_column(col)
            for r in pending:
                t.add_row(
                    r.approval_id[:8],
                    r.risk.value,
                    r.summary,
                    r.command or "-",
                    f"{r.seconds_remaining() // 60} min",
                    "yes" if r.remote_allowed else "local only",
                )
            self.console.print(t)
        elif sub in {"approve", "deny"}:
            if len(args) < 2:
                self.console.print(f"[red]usage: /approvals {sub} <id> [session][/red]")
                return
            match = [r for r in mgr.pending() if r.approval_id.startswith(args[1])]
            if len(match) != 1:
                self.console.print("[red]no unique pending approval with that id[/red]")
                return
            scope = (
                ApprovalScope.SESSION
                if len(args) > 2 and args[2] == "session"
                else ApprovalScope.ONCE
            )
            decision = ApprovalDecision.APPROVE if sub == "approve" else ApprovalDecision.DENY
            try:
                mgr.decide(
                    match[0].approval_id, decision, scope, via="cli", by="terminal", trusted=True
                )
            except ApprovalError as exc:
                self.console.print(f"[red]{exc}[/red]")
        elif sub == "history":
            rows = mgr.history(limit=20)
            t = Table(title="Approval history (this session)")
            for col in ("id", "status", "action", "via", "decided at"):
                t.add_column(col)
            for row in rows:
                t.add_row(
                    row["id"][:8],
                    row["status"],
                    row["summary"],
                    row["decided_via"] or "-",
                    (row["decided_at"] or "")[:19],
                )
            self.console.print(t)
        else:
            self.console.print(
                "[red]usage: /approvals [list|approve <id> [session]|deny <id>|history][/red]"
            )

    async def _model(self, args: list[str]) -> None:
        if not args:
            self.console.print(f"model: {self.app.model_ref}")
            return
        try:
            provider = create_provider(self.app.config, args[0])
        except ProviderError as exc:
            self.console.print(f"[red]{exc}[/red]")
            return
        if self.app.agent:
            self.app.agent.set_provider(provider)
        self.app.model_ref = args[0]
        self.console.print(f"model switched to [bold]{args[0]}[/bold]")

    async def _clear(self, args: list[str]) -> None:
        self.console.clear()
        self._header()

    async def _quit(self, args: list[str]) -> None:
        self._running = False
