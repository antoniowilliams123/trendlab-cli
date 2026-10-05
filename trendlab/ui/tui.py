"""Textual full-screen TUI (spec §29): jet-black, neon-green, event-driven.

A presentation layer over ``TrendLabApp`` and its event bus. No agent logic lives here.
Layout: header (wordmark, model, project, mode, remote, session) · transcript with a live
streaming pane · plan panel · status bar · input · footer.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

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
from trendlab.ui.theme import GREY, MINT, NEON, NEON_DIM, RED, TUI_CSS, WORDMARK, make_console

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class ApprovalModal(ModalScreen[None]):
    BINDINGS = [
        Binding("y", "once", "approve once"),
        Binding("s", "session", "session"),
        Binding("p", "project", "project"),
        Binding("n", "deny", "deny"),
    ]

    def __init__(self, request: ApprovalRequest, decide) -> None:
        super().__init__()
        self.request = request
        self._decide = decide

    def compose(self) -> ComposeResult:
        r = self.request
        classes = "question" if r.kind == "question" else ("high" if r.risk.value == "high" else "")
        with Vertical(id="dialog", classes=classes):
            if r.kind == "question":
                yield Label(f"❓ TrendLab asks: {r.explanation}")
                with Horizontal(id="buttons"):
                    for i, o in enumerate(r.options):
                        yield Button(o, id=f"opt{i}", variant="primary")
                yield Input(placeholder="Type an answer and press Enter", id="answer")
                return
            yield Label(f"⏳ Approval required · risk {r.risk.value.upper()}")
            lines = [f"Action     {r.summary}"]
            if r.command:
                lines.append(f"Command    {r.command}")
            lines.append(f"Directory  {r.cwd}")
            if r.affected_files:
                lines.append(f"Files      {', '.join(r.affected_files[:5])}")
            lines.append(f"Expires    in {max(1, r.seconds_remaining() // 60)} min")
            lines.append(
                "Remote     request sent — you can also decide on your phone"
                if r.remote_allowed
                else "Remote     high-risk: local approval required"
            )
            yield Static("\n".join(lines))
            if r.explanation:
                yield Static(Text(f"Agent says: {r.explanation}", style=GREY))
            if r.preview:
                yield Static(
                    render_diff(r.preview, title="proposed change", max_lines=60), id="preview"
                )
            with Horizontal(id="buttons"):
                yield Button("Approve once  y", id="once", variant="success")
                if r.session_scope_allowed:
                    yield Button("Session  s", id="session")
                    yield Button("Project  p", id="project")
                yield Button("Deny  n", id="deny", variant="error")

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


def _shorten(path: str, limit: int) -> str:
    home = str(Path.home())
    if path.startswith(home):
        path = "~" + path[len(home) :]
    if len(path) <= limit:
        return path
    return "…" + path[-(limit - 1) :]


def _mode_badge(tl: TrendLabApp) -> str:
    if tl.engine.unsafe:
        return "[bold white on #ff3b3b] UNSAFE [/]"
    return f"[bold {NEON}]{tl.engine.mode.value.upper()}[/]"


class TrendLabTUI(App[None]):
    TITLE = PRODUCT_NAME
    CSS = TUI_CSS
    BINDINGS = [
        Binding("ctrl+c", "cancel_or_quit", "cancel / quit", priority=True),
        Binding("ctrl+l", "clear", "clear"),
        Binding("f1", "help", "help"),
        Binding("f2", "plan", "plan"),
        Binding("f3", "cost", "cost"),
    ]

    def __init__(self, tl_app: TrendLabApp) -> None:
        super().__init__()
        self.tl = tl_app
        self._run_task: asyncio.Task | None = None
        self._modals: dict[str, ApprovalModal] = {}
        self._stream_buf: list[str] = []
        self._spin = 0
        self._started_at: float | None = None
        self.console_out = make_console(record=True, width=100, force_terminal=False)
        self.commands = CommandRouter(
            tl_app, self.console_out, quit_cb=self.exit, clear_cb=self._clear_log
        )

    # -- layout ------------------------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                log = RichLog(id="transcript", wrap=True, markup=True, highlight=False)
                log.border_title = "transcript"
                yield log
                yield Static(id="stream")
            plan = Static("(no plan yet)", id="plan")
            plan.border_title = "plan"
            yield plan
        yield Static(id="status")
        yield Input(
            placeholder="Describe a task, or /help  ·  F1 help · F2 plan · F3 cost", id="input"
        )
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
        self._refresh_plan()
        self.query_one("#input", Input).focus()
        self.set_interval(0.25, self._tick)
        self.log_line(
            f"[bold {NEON}]{PRODUCT_NAME}[/] [{GREY}]v{__version__}[/] ready — "
            f"[{NEON_DIM}]type a task, or /help[/]"
        )
        if self.tl.engine.unsafe:
            self.log_line(
                "[bold white on #ff3b3b] UNSAFE [/] [#ff3b3b]approval prompts are off · sudo and "
                "outside-project paths stay denied · destructive commands still ask · "
                "/mode ask turns prompts on[/]"
            )

    async def on_unmount(self) -> None:
        await self.tl.stop()

    # -- rendering ---------------------------------------------------------------------------------
    def log_line(self, text) -> None:
        self.query_one("#transcript", RichLog).write(text)

    def _clear_log(self) -> None:
        self.query_one("#transcript", RichLog).clear()

    def _refresh_header(self) -> None:
        tl = self.tl
        remote = tl.remote_status()
        remote_txt = f"[bold {NEON}]remote ON[/]" if remote["enabled"] else f"[{GREY}]remote off[/]"
        privacy = tl.privacy_label()
        privacy_txt = (
            f"[bold {MINT}]{privacy}[/]" if privacy == "LOCAL" else f"[{GREY}]{privacy}[/]"
        )
        width = max(60, self.size.width - 4)
        project = _shorten(str(tl.project_root), max(20, width - 70))
        line1 = (
            f"[bold {NEON}]{WORDMARK}[/]  [{GREY}]CLI v{__version__}[/]   "
            f"[bold {NEON}]{tl.model_ref}[/] {privacy_txt}"
        )
        line2 = (
            f"[{GREY}]{project}[/]   {_mode_badge(tl)}   {remote_txt}   "
            f"[{GREY}]session {tl.session_id}[/]"
        )
        self.query_one("#header", Static).update(f"{line1}\n{line2}")
        self._refresh_status()

    def _refresh_status(self) -> None:
        tl = self.tl
        cost = tl.costs.total_usd if tl.costs else 0.0
        ctx = tl.context.status() if tl.context else {"estimated_tokens": 0, "context_window": 0}
        state = tl.agent.state.state.value if tl.agent else "-"
        pending = len(tl.approvals.pending()) if tl.approvals else 0
        running = self._run_task is not None and not self._run_task.done()
        spinner = f"[bold {NEON}]{_SPINNER[self._spin % len(_SPINNER)]}[/] " if running else ""
        elapsed = (
            f" [{GREY}]{int(time.monotonic() - self._started_at)}s[/]"
            if running and self._started_at
            else ""
        )
        pct = (
            int(100 * ctx["estimated_tokens"] / ctx["context_window"])
            if ctx.get("context_window")
            else 0
        )
        parts = [
            f"{spinner}[bold {NEON}]{state}[/]{elapsed}",
            f"[{NEON_DIM}]{tl.model_ref}[/]",
            f"[{NEON_DIM}]ctx[/] [bold {NEON}]{ctx['estimated_tokens'] // 1000}k[/] "
            f"[{GREY}]({pct}%)[/]",
            f"[{NEON_DIM}]cost[/] [bold {NEON}]${cost:.3f}[/]",
            _mode_badge(tl),
        ]
        if pending:
            parts.append(f"[bold #ffd21f]⏳ {pending} pending[/]")
        self.query_one("#status", Static).update("  │  ".join(parts))

    def _refresh_plan(self) -> None:
        plan = self.tl.plan
        if not plan.tasks:
            self.query_one("#plan", Static).update(f"[{GREY}](no plan yet)[/]")
            return
        glyph = {
            "pending": f"[{GREY}]○[/]",
            "active": f"[bold {MINT}]→[/]",
            "blocked": "[#ffd21f]![/]",
            "completed": f"[bold {NEON}]✓[/]",
            "failed": f"[{RED}]✗[/]",
            "canceled": f"[{GREY}]-[/]",
        }
        lines = []
        for t in plan.tasks:
            style = GREY if t.status.value in {"completed", "canceled"} else NEON
            lines.append(f"{glyph[t.status.value]} [{style}]{t.title}[/]")
        done = sum(1 for t in plan.tasks if t.status.value == "completed")
        lines.append(f"\n[{NEON_DIM}]{done}/{len(plan.tasks)} done[/]")
        self.query_one("#plan", Static).update("\n".join(lines))

    def _tick(self) -> None:
        if self._run_task is not None and not self._run_task.done():
            self._spin += 1
            self._refresh_status()

    # -- streaming ---------------------------------------------------------------------------------
    def _on_token(self, text: str) -> None:
        self._stream_buf.append(text)
        pane = self.query_one("#stream", Static)
        pane.add_class("visible")
        joined = "".join(self._stream_buf)
        tail = joined[-1500:]
        pane.update(Text(tail, style=NEON))

    def _flush_stream(self) -> None:
        pane = self.query_one("#stream", Static)
        pane.remove_class("visible")
        pane.update("")
        self._stream_buf.clear()

    # -- events ------------------------------------------------------------------------------------
    def _on_event(self, event: Event) -> None:
        if self._in_loop():
            self._handle_event(event)
        else:
            self.call_from_thread(self._handle_event, event)

    def _in_loop(self) -> bool:
        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def _handle_event(self, event: Event) -> None:
        d = event.data
        t = event.type
        if t == EventType.TOOL_STARTED:
            self.log_line(f"[{NEON_DIM}]●[/] [{NEON}]{d.get('summary')}[/]")
        elif t == EventType.TOOL_COMPLETED:
            mark = f"[bold {NEON}]✓[/]" if d.get("ok") else f"[bold {RED}]✗[/]"
            self.log_line(
                f"  {mark} [{NEON_DIM}]{d.get('tool')}[/] [{GREY}]{d.get('duration_ms')} ms[/]"
            )
        elif t == EventType.FILE_CHANGED and d.get("diff"):
            self.log_line(render_diff(d["diff"], title=", ".join(d.get("files", []))))
        elif t == EventType.PLAN_UPDATED:
            self._refresh_plan()
        elif t == EventType.APPROVAL_REQUESTED:
            self.log_line(
                f"[bold #ffd21f]⏳ Waiting for approval[/] [{NEON_DIM}]{d.get('summary')}[/]"
            )
        elif t == EventType.APPROVAL_APPROVED:
            where = "remotely" if d.get("channel") == "web" else "locally"
            self.log_line(f"[bold {NEON}]✓ Approved {where}[/] [{GREY}]— continuing…[/]")
        elif t == EventType.APPROVAL_DENIED:
            where = "remotely" if d.get("channel") == "web" else "locally"
            self.log_line(f"[bold {RED}]✗ Denied {where}[/]")
        elif t == EventType.APPROVAL_EXPIRED:
            self.log_line("[bold #ffd21f]⌛ Approval expired[/]")
        elif t == EventType.SECRET_WRITE_BLOCKED:
            self.log_line(
                f"[bold {RED}]⛔ blocked a write that looked like a secret[/] "
                f"[{GREY}]{d.get('files')}[/]"
            )
        elif t == EventType.RECOVERY and d.get("failure") == "UNVERIFIED_COMPLETION":
            self.log_line("[#ffd21f]✎ asked the model to validate its work before finishing[/]")
        elif t == EventType.CONTEXT_COMPACTED:
            self.log_line(
                f"[#ffd21f]⇅ compacted {d.get('messages_compacted')} messages[/] "
                f"[{GREY}]{d.get('tokens_before')} → {d.get('tokens_after')} tokens[/]"
            )
        elif t in {
            EventType.LOOP_DETECTED,
            EventType.BUDGET_WARNING,
            EventType.PROVIDER_FALLBACK,
            EventType.RECOVERY,
        }:
            self.log_line(
                f"[#ffd21f]{t.value}[/] "
                f"[{GREY}]{d.get('reason') or d.get('error') or d.get('action') or ''}[/]"
            )
        elif t == EventType.MODEL_CALL_STARTED:
            self._flush_stream()
        elif (
            t in {EventType.AGENT_STATE_CHANGED, EventType.COST_UPDATED}
            and d.get("role", "main") == "main"
        ):
            self._refresh_status()

    # -- approvals ---------------------------------------------------------------------------------
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
                self.log_line(f"[#ffd21f]{exc}[/]")

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
            self.log_line(f"[bold {MINT}]Answered:[/] {result.reason}")
        self._refresh_status()

    # -- input -------------------------------------------------------------------------------------
    @on(Input.Submitted, "#input")
    async def _submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            await self._command(text)
            return
        if self._run_task and not self._run_task.done():
            self.log_line("[#ffd21f]A task is already running (Ctrl+C to cancel).[/]")
            return
        self.log_line(f"[bold {MINT}]❯[/] [bold {MINT}]{text}[/]")
        self._started_at = time.monotonic()
        self._run_task = asyncio.create_task(self._run(text))

    async def _command(self, text: str) -> None:
        await self.commands.dispatch(text)
        out = self.console_out.export_text(clear=True)
        if out.strip():
            self.log_line(Text(out.rstrip(), style=NEON))
        self._refresh_header()
        self._refresh_plan()

    async def _run(self, text: str) -> None:
        try:
            result = await self.tl.run_prompt(text)
        except Exception as exc:  # noqa: BLE001 — the TUI must survive runtime errors
            self._flush_stream()
            self.log_line(f"[bold {RED}]error:[/] {exc}")
            return
        self._flush_stream()
        if result.status == "COMPLETED":
            self.log_line(Markdown(result.report))
        else:
            self.log_line(Text(result.report, style="#ffd21f"))
        self._refresh_header()
        self._refresh_plan()

    def action_cancel_or_quit(self) -> None:
        if self._run_task and not self._run_task.done():
            self.tl.agent.cancel()  # type: ignore[union-attr]
            self.log_line("[#ffd21f]Canceling current task… (Ctrl+C again to quit)[/]")
            self._run_task = None
            return
        self.exit()

    def action_clear(self) -> None:
        self._clear_log()

    async def action_help(self) -> None:
        await self._command("/help")

    async def action_plan(self) -> None:
        await self._command("/plan")

    async def action_cost(self) -> None:
        await self._command("/cost")


def run_tui(
    project: Path,
    config: AppConfig,
    *,
    model_ref: str | None,
    permission_mode: PermissionMode | None,
    resume: str | None,
    allow_destructive: bool = False,
) -> None:
    tl = TrendLabApp(
        project,
        config,
        model_ref=model_ref,
        permission_mode=permission_mode,
        resume=resume,
        console=make_console(record=True, width=100, force_terminal=False),
        allow_destructive=allow_destructive,
    )
    TrendLabTUI(tl).run()
