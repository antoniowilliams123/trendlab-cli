"""Textual full-screen TUI (spec §29, §38 of the build plan).

A presentation layer over the same ``TrendLabApp``/event bus the REPL uses:
header · transcript · plan panel · footer · input · approval modal. No agent
logic lives here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, RichLog, Static

from trendlab import PRODUCT_NAME, __version__
from trendlab.app import TrendLabApp
from trendlab.approvals.channels.textual_channel import TextualChannel
from trendlab.approvals.models import (
    ApprovalDecision,
    ApprovalError,
    ApprovalRequest,
    ApprovalScope,
    ApprovalStatus,
    DecisionResult,
)
from trendlab.config.schema import AppConfig, PermissionMode
from trendlab.telemetry.events import Event, EventType
from trendlab.ui.commands import CommandRouter
from trendlab.ui.diff_view import render_diff


class ApprovalModal(ModalScreen[None]):
    DEFAULT_CSS = """
    ApprovalModal { align: center middle; }
    #dialog { width: 90; max-height: 90%; border: thick $warning; background: $surface;
              padding: 1 2; }
    #buttons { height: 3; margin-top: 1; }
    #buttons Button { margin-right: 1; }
    #preview { max-height: 20; }
    """

    def __init__(self, request: ApprovalRequest, decide) -> None:
        super().__init__()
        self.request = request
        self._decide = decide

    def compose(self) -> ComposeResult:
        r = self.request
        with Vertical(id="dialog"):
            if r.kind == "question":
                yield Label(f"❓ {r.explanation}")
                for i, o in enumerate(r.options):
                    yield Button(o, id=f"opt{i}", variant="primary")
                yield Input(placeholder="Type an answer and press Enter", id="answer")
                return
            yield Label(f"⏳ Waiting for approval — risk {r.risk.value.upper()}")
            yield Static(
                f"Action: {r.summary}\nCommand: {r.command or '-'}\nDirectory: {r.cwd}\n"
                f"Files: {', '.join(r.affected_files[:5]) or '-'}\n"
                f"Expires in {max(1, r.seconds_remaining() // 60)} min"
                + (
                    "\nRemote approval request sent — you can also decide on your phone."
                    if r.remote_allowed
                    else "\nHigh-risk: local approval required."
                )
            )
            if r.explanation:
                yield Static(Text(f"Agent says: {r.explanation}", style="dim"))
            if r.preview:
                yield Static(
                    render_diff(r.preview, title="proposed change", max_lines=60), id="preview"
                )
            with Horizontal(id="buttons"):
                yield Button("Approve once (y)", id="once", variant="success")
                if r.session_scope_allowed:
                    yield Button("Session (s)", id="session")
                    yield Button("Project (p)", id="project")
                yield Button("Deny (n)", id="deny", variant="error")

    BINDINGS = [
        Binding("y", "once", "approve"),
        Binding("s", "session", "session"),
        Binding("p", "project", "project"),
        Binding("n", "deny", "deny"),
    ]

    def action_once(self) -> None:
        self._decide(ApprovalDecision.APPROVE, ApprovalScope.ONCE)

    def action_session(self) -> None:
        if self.request.session_scope_allowed:
            self._decide(ApprovalDecision.APPROVE, ApprovalScope.SESSION)

    def action_project(self) -> None:
        if self.request.session_scope_allowed:
            self._decide(ApprovalDecision.APPROVE, ApprovalScope.PROJECT)

    def action_deny(self) -> None:
        self._decide(ApprovalDecision.DENY, ApprovalScope.ONCE)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("opt"):
            self._decide("answer", self.request.options[int(bid[3:])])
            return
        {
            "once": self.action_once,
            "session": self.action_session,
            "project": self.action_project,
            "deny": self.action_deny,
        }[bid]()

    @on(Input.Submitted, "#answer")
    def _answer(self, event: Input.Submitted) -> None:
        if event.value.strip():
            self._decide("answer", event.value.strip())


class TrendLabTUI(App[None]):
    TITLE = PRODUCT_NAME
    CSS = """
    #header { height: 3; background: $primary-background; padding: 0 1; }
    #body { height: 1fr; }
    #transcript { width: 3fr; border: round $secondary; }
    #plan { width: 1fr; border: round $secondary; padding: 0 1; }
    #status { height: 1; padding: 0 1; background: $panel; }
    #input { dock: bottom; }
    """
    BINDINGS = [
        Binding("ctrl+c", "cancel_or_quit", "cancel/quit", priority=True),
        Binding("ctrl+l", "clear", "clear"),
    ]

    def __init__(self, tl_app: TrendLabApp) -> None:
        super().__init__()
        self.tl = tl_app
        self._run_task: asyncio.Task | None = None
        self._modals: dict[str, ApprovalModal] = {}
        self._stream_buf: list[str] = []
        self.console_out = Console(record=True, width=100, force_terminal=False)
        self.commands = CommandRouter(
            tl_app, self.console_out, quit_cb=self.exit, clear_cb=self._clear_log
        )
        self._last_sigint = 0.0

    # -- layout ----------------------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with Horizontal(id="body"):
            yield RichLog(id="transcript", wrap=True, markup=True, highlight=False)
            yield Static("Plan\n(no plan yet)", id="plan")
        yield Static(id="status")
        yield Input(placeholder="Describe a task, or /help", id="input")
        yield Footer()

    async def on_mount(self) -> None:
        channel = TextualChannel(self._present_approval, self._withdraw_approval)
        self.tl.on_token = self._on_token
        await self.tl.start(interactive=False, command_handler=self.commands.dispatch)
        await self.tl.approvals.add_channel(channel)  # type: ignore[union-attr]
        self.tl.agent.on_token = self._on_token  # type: ignore[union-attr]
        self.tl.agent.stream = True  # type: ignore[union-attr]
        self.tl.events.subscribe(self._on_event)
        self._refresh_header()
        self.query_one("#input", Input).focus()
        self.log_line(f"[bold]{PRODUCT_NAME}[/bold] v{__version__} ready. Type a task or /help.")

    async def on_unmount(self) -> None:
        await self.tl.stop()

    # -- rendering -------------------------------------------------------------------------------
    def log_line(self, text) -> None:
        self.query_one("#transcript", RichLog).write(text)

    def _clear_log(self) -> None:
        self.query_one("#transcript", RichLog).clear()

    def _refresh_header(self) -> None:
        tl = self.tl
        remote = tl.remote_status()
        self.query_one("#header", Static).update(
            f"[bold]{PRODUCT_NAME}[/bold]  {tl.model_ref} [{tl.privacy_label()}]\n"
            f"{tl.project_root}   mode {tl.engine.mode.value.upper()}   "
            f"remote {'ON' if remote['enabled'] else 'off'}   session {tl.session_id}"
        )
        self._refresh_status()

    def _refresh_status(self) -> None:
        tl = self.tl
        cost = tl.costs.total_usd if tl.costs else 0.0
        ctx = tl.context.status() if tl.context else {"estimated_tokens": 0}
        state = tl.agent.state.state.value if tl.agent else "-"
        pending = len(tl.approvals.pending()) if tl.approvals else 0
        self.query_one("#status", Static).update(
            f"{tl.model_ref} │ ~{ctx['estimated_tokens'] // 1000}k ctx │ ${cost:.3f} │ "
            f"{tl.engine.mode.value.upper()} │ {state}"
            + (f" │ ⏳ {pending} pending" if pending else "")
        )

    def _refresh_plan(self) -> None:
        self.query_one("#plan", Static).update(self.tl.plan.render())

    def _on_token(self, text: str) -> None:
        self._stream_buf.append(text)

    def _flush_stream(self) -> None:
        if self._stream_buf:
            self.log_line(Text("".join(self._stream_buf)))
            self._stream_buf.clear()

    def _on_event(self, event: Event) -> None:
        self.call_from_thread(
            self._handle_event, event
        ) if not self._in_loop() else self._handle_event(event)

    def _in_loop(self) -> bool:
        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def _handle_event(self, event: Event) -> None:
        d = event.data
        t = event.type
        if t == EventType.TOOL_STARTED:
            self.log_line(f"[dim]●[/dim] {d.get('summary')}")
        elif t == EventType.TOOL_COMPLETED:
            mark = "[green]✓[/green]" if d.get("ok") else "[red]✗[/red]"
            self.log_line(f"  {mark} {d.get('tool')} ({d.get('duration_ms')} ms)")
        elif t == EventType.FILE_CHANGED and d.get("diff"):
            self.log_line(render_diff(d["diff"], title=", ".join(d.get("files", []))))
        elif t == EventType.PLAN_UPDATED:
            self._refresh_plan()
        elif t == EventType.APPROVAL_APPROVED:
            self.log_line(
                f"[green]✓ Approved {'remotely' if d.get('channel') == 'web' else 'locally'}"
                "[/green] — continuing…"
            )
        elif t == EventType.APPROVAL_DENIED:
            self.log_line(
                f"[red]✗ Denied {'remotely' if d.get('channel') == 'web' else 'locally'}[/red]"
            )
        elif t == EventType.APPROVAL_EXPIRED:
            self.log_line("[yellow]⌛ Approval expired[/yellow]")
        elif t in {EventType.LOOP_DETECTED, EventType.BUDGET_WARNING, EventType.PROVIDER_FALLBACK}:
            self.log_line(f"[yellow]{t.value}: {d.get('reason') or d.get('error') or d}[/yellow]")
        elif t == EventType.AGENT_STATE_CHANGED and d.get("role", "main") == "main":
            self._refresh_status()
        elif t == EventType.COST_UPDATED:
            self._refresh_status()

    # -- approvals ------------------------------------------------------------------------------
    def _present_approval(self, request: ApprovalRequest) -> None:
        def decide(decision, scope) -> None:
            try:
                if decision == "answer":
                    self.tl.approvals.answer(
                        request.approval_id, scope, via="local", by="tui", trusted=True
                    )  # type: ignore[union-attr]
                else:
                    self.tl.approvals.decide(
                        request.approval_id, decision, scope, via="local", by="tui", trusted=True
                    )  # type: ignore[union-attr]
            except ApprovalError as exc:
                self.log_line(f"[yellow]{exc}[/yellow]")

        modal = ApprovalModal(request, decide)
        self._modals[request.approval_id] = modal
        self.push_screen(modal)
        self._refresh_status()

    def _withdraw_approval(self, request: ApprovalRequest, result: DecisionResult) -> None:
        modal = self._modals.pop(request.approval_id, None)
        if modal is not None and modal.is_attached:
            try:
                modal.dismiss(None)
            except Exception:  # noqa: BLE001
                pass
        if request.kind == "question" and result.status == ApprovalStatus.APPROVED:
            self.log_line(f"[cyan]Answered:[/cyan] {result.reason}")
        self._refresh_status()

    # -- input -------------------------------------------------------------------------------------
    @on(Input.Submitted, "#input")
    async def _submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            await self.commands.dispatch(text)
            out = self.console_out.export_text(clear=True)
            if out.strip():
                self.log_line(Text(out.rstrip()))
            self._refresh_header()
            self._refresh_plan()
            return
        if self._run_task and not self._run_task.done():
            self.log_line("[yellow]A task is already running (Ctrl+C to cancel).[/yellow]")
            return
        self.log_line(f"[bold cyan]>[/bold cyan] {text}")
        self._run_task = asyncio.create_task(self._run(text))

    async def _run(self, text: str) -> None:
        try:
            result = await self.tl.run_prompt(text)
        except Exception as exc:  # noqa: BLE001 — the TUI must survive runtime errors
            self.log_line(f"[red]error: {exc}[/red]")
            return
        self._flush_stream()
        self.log_line(
            Markdown(result.report) if result.status == "COMPLETED" else Text(result.report)
        )
        self._refresh_header()
        self._refresh_plan()

    def action_cancel_or_quit(self) -> None:
        import time

        now = time.monotonic()
        if self._run_task and not self._run_task.done():
            self.tl.agent.cancel()  # type: ignore[union-attr]
            self.log_line("[yellow]Canceling current task…[/yellow]")
            self._last_sigint = now
            return
        self.exit()

    def action_clear(self) -> None:
        self._clear_log()


def run_tui(
    project: Path,
    config: AppConfig,
    *,
    model_ref: str | None,
    permission_mode: PermissionMode | None,
    resume: str | None,
) -> None:
    tl = TrendLabApp(
        project,
        config,
        model_ref=model_ref,
        permission_mode=permission_mode,
        resume=resume,
        console=Console(record=True, width=100, force_terminal=False),
    )
    TrendLabTUI(tl).run()
