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
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Paste
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, OptionList, RichLog, Static, TextArea

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
from trendlab.providers.catalog import ModelChoice, looks_experimental, supports_vision
from trendlab.telemetry.events import Event, EventType
from trendlab.ui.activity import format_event, run_footer
from trendlab.ui.commands import CommandRouter
from trendlab.ui.diff_view import render_diff
from trendlab.ui.file_refs import FileIndex, current_at_token
from trendlab.ui.history import PromptHistory
from trendlab.ui.prompt_rules import continues_line
from trendlab.ui.theme import (
    BANNER,
    BANNER_WIDTH,
    GREY,
    MINT,
    NEON,
    NEON_DIM,
    NEON_SOFT,
    RED,
    TUI_CSS,
    make_console,
)

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class ApprovalModal(ModalScreen[None]):
    BINDINGS = [
        Binding("y", "once", "approve once"),
        Binding("s", "session", "session"),
        Binding("p", "project", "project"),
        Binding("n", "deny", "deny"),
        Binding("j", "preview_down", "scroll diff", show=False),
        Binding("k", "preview_up", "scroll diff", show=False),
        Binding("pagedown", "preview_page_down", "page", show=False),
        Binding("pageup", "preview_page_up", "page", show=False),
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
                n_lines = len(r.preview.splitlines())
                title = f"proposed change · {n_lines} lines"
                if n_lines > 18:
                    title += " · j/k · PgUp/PgDn · wheel to scroll"
                with VerticalScroll(id="preview"):
                    yield Static(render_diff(r.preview, title=title, max_lines=2000))
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

    def _preview(self) -> VerticalScroll | None:
        try:
            return self.query_one("#preview", VerticalScroll)
        except Exception:  # noqa: BLE001 — no diff on this request
            return None

    def action_preview_down(self) -> None:
        if (pv := self._preview()) is not None:
            pv.scroll_relative(y=3, animate=False)

    def action_preview_up(self) -> None:
        if (pv := self._preview()) is not None:
            pv.scroll_relative(y=-3, animate=False)

    def action_preview_page_down(self) -> None:
        if (pv := self._preview()) is not None:
            pv.scroll_page_down(animate=False)

    def action_preview_page_up(self) -> None:
        if (pv := self._preview()) is not None:
            pv.scroll_page_up(animate=False)

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


class PromptInput(TextArea):
    """Multi-line prompt: Enter submits; Shift+Enter, Ctrl+J or a trailing backslash add a line."""

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    BINDINGS = [
        Binding("enter", "submit_prompt", "send", show=False, priority=True),
        Binding("shift+enter", "newline", "newline", show=False, priority=True),
        Binding("ctrl+j", "newline", "newline", show=False, priority=True),
        Binding("tab", "pick_or_indent", "pick file", show=False, priority=True),
        Binding("up", "pick_up", "", show=False, priority=True),
        Binding("down", "pick_down", "", show=False, priority=True),
        Binding("pageup", "transcript_page_up", "", show=False, priority=True),
        Binding("pagedown", "transcript_page_down", "", show=False, priority=True),
        Binding("ctrl+up", "history_prev", "", show=False, priority=True),
        Binding("ctrl+down", "history_next", "", show=False, priority=True),
        Binding("ctrl+v", "paste_image", "paste image", show=False, priority=True),
        # Windows Terminal keeps Ctrl+V for its own paste and swallows it when the clipboard
        # holds an image, so give the same action a key the terminal does not intercept.
        Binding("alt+v", "paste_image", "paste image", show=False, priority=True),
        Binding("ctrl+i", "paste_image", "paste image", show=False, priority=True),
    ]

    def action_paste_image(self) -> None:
        """Ctrl+V / Alt+V: attach the clipboard image; if the clipboard holds text instead,
        insert it (terminals that pass Ctrl+V through do not paste for us)."""
        from trendlab.ui.attachments import grab_clipboard_image, grab_clipboard_text

        path = grab_clipboard_image()
        if path is not None:
            self.post_message(self.ImagePasted(path))
            return
        text = grab_clipboard_text()
        if text:
            self.insert(text)
            return
        self.app.notify("clipboard is empty", severity="warning", timeout=3)  # type: ignore[attr-defined]

    class ImagePasted(Message):
        """An image landed in the prompt (clipboard image, or a pasted path to an image file)."""

        def __init__(self, path: Path) -> None:
            super().__init__()
            self.path = path

    async def _on_paste(self, event: Paste) -> None:
        if not (event.text or "").strip():
            # Some terminals send an empty paste when the clipboard holds an image.
            event.stop()
            event.prevent_default()
            self.action_paste_image()
            return
        text = (event.text or "").strip().strip("'\"")
        if text.startswith("file://"):
            text = text[7:]
        candidate = Path(text).expanduser()
        if (
            text
            and "\n" not in text
            and candidate.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}
            and candidate.is_file()
        ):
            event.stop()
            event.prevent_default()
            self.post_message(self.ImagePasted(candidate.resolve()))

    class PickRequested(Message):
        """Posted when the word under the cursor is an ``@`` file reference."""

        def __init__(self, query: str | None) -> None:
            super().__init__()
            self.query = query

    def _at_token(self) -> tuple[int, str] | None:
        row, col = self.cursor_location
        return current_at_token(self.document.get_line(row), col)

    def _on_text_area_changed(self, event: TextArea.Changed) -> None:
        tok = self._at_token()
        self.post_message(self.PickRequested(tok[1] if tok else None))
        text = self.text
        slash = text if text.startswith("/") and "\n" not in text and " " not in text else None
        self.post_message(self.CommandMenuRequested(slash))

    class CommandMenuRequested(Message):
        """Posted while a bare ``/command`` is being typed (None closes the menu)."""

        def __init__(self, query: str | None) -> None:
            super().__init__()
            self.query = query

    def _cmd_menu(self) -> CommandPicker | None:
        try:
            menu = self.app.query_one("#cmdmenu", CommandPicker)
        except Exception:  # noqa: BLE001
            return None
        return menu if menu.has_class("visible") else None

    def _history(self) -> PromptHistory | None:
        return getattr(self.app, "history", None)

    def _mouse_captured(self) -> bool:
        return bool(getattr(self.app, "mouse_capture", False))

    def _set_text(self, text: str) -> None:
        self.text = text
        self.move_cursor(self.document.end)

    def action_history_prev(self) -> None:
        hist = self._history()
        if hist is None:
            return
        prev = hist.previous(self.text)
        if prev is not None:
            self._set_text(prev)

    def action_history_next(self) -> None:
        hist = self._history()
        if hist is None:
            return
        nxt = hist.next()
        if nxt is not None:
            self._set_text(nxt)

    def _picker(self) -> FilePicker | None:
        try:
            picker = self.app.query_one("#picker", FilePicker)
        except Exception:  # noqa: BLE001
            return None
        return picker if picker.has_class("visible") else None

    def accept_pick(self, path: str) -> None:
        tok = self._at_token()
        if tok is None:
            return
        row, col = self.cursor_location
        start, token = tok
        self.replace(f"@{path} ", (row, start), (row, start + len(token)))
        self.post_message(self.PickRequested(None))

    def action_pick_or_indent(self) -> None:
        menu = self._cmd_menu()
        if menu is not None and menu.choice():
            self._set_text(menu.choice() + " ")
            self.post_message(self.CommandMenuRequested(None))
            return
        picker = self._picker()
        if picker is not None and picker.choice():
            self.accept_pick(picker.choice())  # type: ignore[arg-type]
            return
        self.insert("\t" if self.indent_type == "tabs" else " " * self.indent_width)

    def _scroll_transcript(self, lines: int) -> bool:
        scroller = getattr(self.app, "scroll_transcript", None)
        if scroller is None:
            return False
        scroller(lines)
        return True

    def action_pick_up(self) -> None:
        menu = self._cmd_menu()
        if menu is not None:
            menu.action_cursor_up()
            return
        picker = self._picker()
        if picker is not None:
            picker.action_cursor_up()
            return
        if self.cursor_location[0] == 0:
            # Mouse captured → the wheel is a real mouse event, so ↑ can be history (hosted-agent
            # style). Mouse released → the wheel arrives as ↑ and must scroll the transcript;
            # history is on Ctrl+↑ in that mode.
            if self._mouse_captured() and self.document.line_count == 1:
                self.action_history_prev()
                return
            if self._scroll_transcript(-3):
                return
        self.action_cursor_up()

    def action_pick_down(self) -> None:
        menu = self._cmd_menu()
        if menu is not None:
            menu.action_cursor_down()
            return
        picker = self._picker()
        if picker is not None:
            picker.action_cursor_down()
            return
        if self.cursor_location[0] >= self.document.line_count - 1:
            hist = self._history()
            if self._mouse_captured() and self.document.line_count == 1 and hist and hist.browsing:
                self.action_history_next()
                return
            if self._scroll_transcript(3):
                return
        self.action_cursor_down()

    def action_transcript_page_up(self) -> None:
        self._scroll_transcript(-1000)

    def action_transcript_page_down(self) -> None:
        self._scroll_transcript(1000)

    def action_submit_prompt(self) -> None:
        menu = self._cmd_menu()
        if menu is not None and menu.choice() and menu.choice() != self.text.strip():
            self._set_text(menu.choice() + " ")
            self.post_message(self.CommandMenuRequested(None))
            return
        picker = self._picker()
        if picker is not None and picker.choice():
            self.accept_pick(picker.choice())  # type: ignore[arg-type]
            return
        text = self.text
        if continues_line(text):
            # "... \<Enter>" continues on the next line (works in every terminal). A backslash
            # glued to a word (a pasted Windows path like C:\x\results\) is just text.
            self.text = text.rstrip()[:-1].rstrip() + "\n"
            self.move_cursor(self.document.end)
            return
        self.post_message(self.Submitted(text))

    def action_newline(self) -> None:
        self.insert("\n")


class ModelPicker(ModalScreen[str | None]):
    """Model switcher: type to filter, ↑↓ to move, Enter to switch, Esc keeps the current."""

    BINDINGS = [
        Binding("escape", "cancel", "keep current", priority=True),
        Binding("enter", "choose", "switch", priority=True),
        Binding("up", "move(-1)", "", show=False, priority=True),
        Binding("down", "move(1)", "", show=False, priority=True),
    ]

    def __init__(self, choices: list[ModelChoice]) -> None:
        super().__init__()
        self.choices = choices
        self.shown: list[ModelChoice] = list(choices)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="models"):
            yield Label("Switch model — conversation, plan and session are kept")
            yield Input(
                placeholder="type to filter · ↑↓ move · Enter switch · Esc keep", id="model-filter"
            )
            yield OptionList(id="model-list")
            yield Static("", id="model-detail")

    def on_mount(self) -> None:
        self._fill("")
        self.query_one("#model-filter", Input).focus()

    def _row(self, c: ModelChoice) -> Text:
        t = Text(no_wrap=True, overflow="ellipsis")
        ref = c.ref if len(c.ref) <= 36 else c.ref[:35] + "…"
        t.append("● " if c.current else "  ", style=f"bold {NEON}" if c.current else "")
        t.append(f"{ref:<37}", style=f"bold {NEON}" if c.current else NEON)
        t.append(f"{c.ctx_label:<9}", style=GREY)
        t.append(f"{c.price_label:<21}", style=NEON_DIM)
        status = c.status_label
        bad = "missing" in status or "not pulled" in status
        t.append(f"{status:<12}", style=RED if bad else MINT)
        if c.note:
            t.append(c.note[:22], style=GREY)
        return t

    def _fill(self, query: str) -> None:
        lst = self.query_one("#model-list", OptionList)
        lst.clear_options()
        self.shown = [c for c in self.choices if c.matches(query)]
        lst.add_options([self._row(c) for c in self.shown])
        if self.shown:
            lst.highlighted = 0
        self._detail()

    def _detail(self) -> None:
        lst = self.query_one("#model-list", OptionList)
        idx = lst.highlighted if lst.highlighted is not None else 0
        box = self.query_one("#model-detail", Static)
        if not self.shown:
            box.update(Text("no model matches that filter", style=RED))
            return
        c = self.shown[min(idx, len(self.shown) - 1)]
        where = "runs on this machine — nothing leaves it" if c.local else "remote API"
        warn = ""
        if not c.local and not c.key_ok:
            provider = c.provider
            env = ""
            pcfg = self.app.tl.config.providers.get(provider)  # type: ignore[attr-defined]
            if pcfg is not None:
                env = pcfg.api_key_env or ""
            warn = f"\nkey missing — run: trendlab secret set {env}"
        elif c.pulled is False:
            warn = f"\nnot pulled yet — run: ollama pull {c.model}"
        box.update(
            Text(f"{c.ref} · {where} · {c.ctx_label} · {c.price_label}{warn}", style=NEON_SOFT)
        )

    @on(Input.Changed, "#model-filter")
    def _filter(self, event: Input.Changed) -> None:
        self._fill(event.value)

    @on(OptionList.OptionHighlighted, "#model-list")
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self._detail()

    @on(OptionList.OptionSelected, "#model-list")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.action_choose()

    def action_move(self, delta: int) -> None:
        lst = self.query_one("#model-list", OptionList)
        if not self.shown:
            return
        cur = lst.highlighted if lst.highlighted is not None else 0
        lst.highlighted = max(0, min(len(self.shown) - 1, cur + delta))
        self._detail()

    def action_choose(self) -> None:
        lst = self.query_one("#model-list", OptionList)
        if not self.shown:
            return
        idx = lst.highlighted if lst.highlighted is not None else 0
        self.dismiss(self.shown[min(idx, len(self.shown) - 1)].ref)

    def action_cancel(self) -> None:
        self.dismiss(None)


class CommandPicker(OptionList):
    """Slash-command menu shown above the prompt while ``/…`` is typed."""

    def __init__(self, commands: list[tuple[str, str]]) -> None:
        super().__init__(id="cmdmenu")
        self.commands = commands  # (name, description)
        self._matches: list[tuple[str, str]] = []

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        self.commands = commands

    def update_query(self, query: str | None) -> None:
        if query is None:
            self.hide()
            return
        q = query.lower()
        self._matches = [c for c in self.commands if c[0].startswith(q)][:10]
        if not self._matches:
            self._matches = [c for c in self.commands if q.lstrip("/") in c[0]][:10]
        if not self._matches:
            self.hide()
            return
        self.clear_options()
        self.add_options(
            [Text.assemble((f"{n:<14}", f"bold {NEON}"), (d, GREY)) for n, d in self._matches]
        )
        self.highlighted = 0
        self.border_title = f"commands · {len(self._matches)} · Tab/Enter completes"
        self.add_class("visible")

    def hide(self) -> None:
        self.remove_class("visible")
        self._matches = []

    def choice(self) -> str | None:
        if not self._matches:
            return None
        idx = self.highlighted if self.highlighted is not None else 0
        return self._matches[min(idx, len(self._matches) - 1)][0]


class FilePicker(OptionList):
    """Fuzzy ``@file`` suggestions shown above the prompt while an ``@`` token is being typed."""

    def __init__(self, index: FileIndex) -> None:
        super().__init__(id="picker")
        self.index = index
        self._matches: list[str] = []

    def update_query(self, query: str | None) -> None:
        if query is None:
            self.hide()
            return
        self._matches = self.index.search(query, limit=8)
        if not self._matches:
            self.hide()
            return
        self.clear_options()
        self.add_options(self._matches)
        self.highlighted = 0
        self.border_title = f"@ files · {len(self.index.files())} indexed · Tab/Enter picks"
        self.add_class("visible")

    def hide(self) -> None:
        self.remove_class("visible")
        self._matches = []

    def choice(self) -> str | None:
        if not self._matches:
            return None
        idx = self.highlighted if self.highlighted is not None else 0
        return self._matches[min(idx, len(self._matches) - 1)]


class TrendLabTUI(App[None]):
    TITLE = PRODUCT_NAME
    CSS = TUI_CSS
    ENABLE_COMMAND_PALETTE = False  # Textual's own palette (themes, screenshots) is not ours
    BINDINGS = [
        Binding("ctrl+c", "cancel_or_quit", "cancel / quit", priority=True),
        Binding("escape", "interrupt", "interrupt", priority=True),
        Binding("ctrl+l", "clear", "clear"),
        Binding("f1", "help", "help"),
        Binding("f2", "plan", "plan"),
        Binding("f3", "cost", "cost"),
        Binding("f4", "toggle_mouse", "mouse"),
        Binding("f5", "pick_model", "model"),
        Binding("ctrl+e", "external_editor", "editor"),
    ]

    def __init__(self, tl_app: TrendLabApp) -> None:
        super().__init__()
        self.tl = tl_app
        # Like a plain CLI, the terminal keeps the mouse by default: drag selects text and the
        # terminal's own copy (Ctrl+Shift+C / right-click) works. F4 or /mouse on hands the mouse
        # to the app for wheel scrolling and clicking.
        self.mouse_capture = False
        self._run_task: asyncio.Task | None = None
        self._modals: dict[str, ApprovalModal] = {}
        self._stream_buf: list[str] = []
        self._think_buf: list[str] = []
        self._spin = 0
        self._started_at: float | None = None
        self.console_out = make_console(record=True, width=100, force_terminal=False)
        self.commands = CommandRouter(
            tl_app,
            self.console_out,
            quit_cb=self.exit,
            clear_cb=self._clear_log,
            prompt_cb=self._start_prompt,
            model_picker_cb=self._open_model_picker,
        )
        self.commands.register("/mouse", self._mouse_command)
        self.file_index = FileIndex(tl_app.project_root)
        self.history = PromptHistory(tl_app.project_root)

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
        menu = CommandPicker(self._command_catalog())
        menu.border_title = "commands"
        yield menu
        picker = FilePicker(self.file_index)
        picker.border_title = "@ files"
        yield picker
        yield PromptInput(
            id="input",
            show_line_numbers=False,
            soft_wrap=True,
            tab_behavior="indent",
            placeholder="type a task · / commands · @ files · Alt+V image · Ctrl+↑↓ history · Esc",
        )
        yield Footer()

    async def on_mount(self) -> None:
        channel = TextualChannel(self._present_approval, self._withdraw_approval)
        self.tl.on_token = self._on_token
        self.tl.on_thinking = self._on_thinking
        self.tl.on_remote_prompt = self._start_prompt
        await self.tl.start(interactive=False, command_handler=self.commands.dispatch)
        await self.tl.approvals.add_channel(channel)  # type: ignore[union-attr]
        self.tl.agent.on_token = self._on_token  # type: ignore[union-attr]
        self.tl.agent.on_thinking = self._on_thinking  # type: ignore[union-attr]
        self.tl.agent.stream = True  # type: ignore[union-attr]
        self.tl.events.subscribe(self._on_event)
        self._refresh_header()
        self._refresh_plan()
        self.query_one("#input", PromptInput).focus()
        self.set_mouse_capture(False)
        self._apply_layout()
        self.run_worker(self._update_check, thread=True, exclusive=False, name="update-check")
        self.set_interval(0.25, self._tick)
        self.log_line(
            f"[bold {NEON}]{PRODUCT_NAME}[/] [{GREY}]v{__version__}[/] ready — "
            f"[{NEON_DIM}]type a task, or /help[/]"
        )
        if self.tl.engine.unsafe:
            self.log_line(
                "[bold white on #ff3b3b] UNSAFE [/] [#ff3b3b]no approval prompts · sudo and "
                "outside-project paths denied · destructive still asks · /mode ask for prompts[/]"
            )

    async def on_unmount(self) -> None:
        self._write_terminal("\x1b[?1007l")  # leave alternate-scroll mode tidy
        await self.tl.stop()

    # -- mouse / selection ----------------------------------------------------------------------
    def _write_terminal(self, seq: str) -> None:
        driver = self._driver
        if driver is None or getattr(driver, "is_headless", False):
            return
        try:
            driver.write(seq)
            driver.flush()
        except Exception:  # noqa: BLE001 — cosmetic; never break the app over a terminal quirk
            pass

    def set_mouse_capture(self, on: bool) -> None:
        """Hand the mouse to the app (wheel + clicks) or back to the terminal (select + copy)."""
        self.mouse_capture = on
        driver = self._driver
        if driver is not None and hasattr(driver, "_enable_mouse_support"):
            try:
                if on:
                    driver._mouse = True
                    driver._enable_mouse_support()
                else:
                    driver._disable_mouse_support()
                    driver._mouse = False  # stays off across $EDITOR suspend/resume too
            except Exception:  # noqa: BLE001
                pass
        # Alternate-scroll: with the mouse released, terminals (Windows Terminal, xterm, kitty)
        # turn wheel movement into ↑/↓ keys, which the prompt turns into transcript scrolling.
        self._write_terminal("\x1b[?1007h" if not on else "\x1b[?1007l")
        if self.is_mounted:
            self._refresh_status()

    def action_toggle_mouse(self) -> None:
        self.set_mouse_capture(not self.mouse_capture)
        self.log_line(
            f"[{GREY}]mouse captured by TrendLab — wheel scrolls, buttons click; F4 releases it[/]"
            if self.mouse_capture
            else f"[{GREY}]mouse released — drag to select, copy with your terminal "
            f"(Ctrl+Shift+C / right-click); ↑↓ PgUp PgDn scroll the transcript; F4 captures[/]"
        )

    async def _mouse_command(self, args: list[str]) -> None:
        if args and args[0] in {"on", "off"}:
            self.set_mouse_capture(args[0] == "on")
        state = "on (app has the mouse)" if self.mouse_capture else "off (terminal selects/copies)"
        self.console_out.print(f"mouse {state} · F4 toggles")

    def scroll_transcript(self, lines: int) -> None:
        log = self.query_one("#transcript", RichLog)
        if lines in {1000, -1000}:
            (log.scroll_page_down if lines > 0 else log.scroll_page_up)(animate=False)
        else:
            log.scroll_relative(y=lines, animate=False)

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
        project = _shorten(str(tl.project_root), max(20, width - BANNER_WIDTH - 12))
        if looks_experimental(tl.config, tl.model_ref):
            privacy_txt += " [bold #ffd21f]⚠ small local model — unreliable for tasks[/]"
        info = [
            f"[{GREY}]CLI v{__version__}[/]",
            f"[bold {NEON}]{tl.model_ref}[/] {privacy_txt}",
            f"[{GREY}]{project}[/]",
            f"{_mode_badge(tl)}   {remote_txt}",
            f"[{GREY}]session {tl.session_id}[/]",
        ]
        header = self.query_one("#header", Static)
        if header.has_class("compact"):
            header.update(
                f"[bold {NEON}]TRENDLAB[/] [{GREY}]v{__version__}[/]  ·  {info[1]}  ·  "
                f"[{GREY}]{project}[/]  ·  {_mode_badge(tl)}  {remote_txt}"
            )
        else:
            lines = [f"[bold {NEON}]{row}[/]   {info[i]}" for i, row in enumerate(BANNER)]
            header.update("\n".join(lines))
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
        if looks_experimental(tl.config, tl.model_ref):
            parts.append("[bold black on #ffd21f] ⚠ EXPERIMENTAL MODEL [/]")
        if tl.pending_images:
            parts.append(f"[bold {MINT}]📎 {len(tl.pending_images)} image(s)[/]")
        width = self.size.width
        if width >= 110:
            mouse = "app" if self.mouse_capture else "terminal · drag selects"
            parts.append(f"[{NEON_DIM}]mouse[/] [{GREY}]{mouse}[/]")
        elif width < 90:
            parts = [p for p in parts if tl.model_ref not in p]  # the header shows the model
        self.query_one("#status", Static).update("  │  ".join(parts))

    def _refresh_plan(self) -> None:
        plan = self.tl.plan
        panel = self.query_one("#plan", Static)
        panel.display = bool(plan.tasks) and self.size.width >= 100
        if not plan.tasks:
            panel.update(f"[{GREY}](no plan yet)[/]")
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
        self._render_stream()

    def _on_thinking(self, text: str) -> None:
        self._think_buf.append(text)
        self._render_stream()

    def _render_stream(self) -> None:
        pane = self.query_one("#stream", Static)
        pane.add_class("visible")
        out = Text()
        if self._think_buf and not self._stream_buf:
            thinking = "".join(self._think_buf)[-900:]
            out.append("💭 thinking  ", style=f"bold {GREY}")
            out.append(thinking, style=f"italic {GREY}")
        elif self._stream_buf:
            out.append("".join(self._stream_buf)[-1500:], style=NEON)
        pane.update(out)

    def _flush_stream(self) -> None:
        pane = self.query_one("#stream", Static)
        pane.remove_class("visible")
        pane.update("")
        self._stream_buf.clear()
        self._think_buf.clear()

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
        if t == EventType.FILE_CHANGED and d.get("diff"):
            self.log_line(render_diff(d["diff"], title=", ".join(d.get("files", []))))
            return
        if t == EventType.PLAN_UPDATED:
            self._refresh_plan()
            return
        if t == EventType.MODEL_CALL_STARTED:
            self._flush_stream()
            return
        if (
            t in {EventType.AGENT_STATE_CHANGED, EventType.COST_UPDATED}
            and d.get("role", "main") == "main"
        ):
            self._refresh_status()
            return
        if d.get("role", "main") != "main" and t in {
            EventType.TOOL_STARTED,
            EventType.TOOL_COMPLETED,
            EventType.TOOL_SKIPPED,
        }:
            return  # sub-agent activity is summarised by the delegate tool's own line
        line = format_event(event)
        if line:
            self.log_line(line)

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
    @on(PromptInput.Submitted)
    async def _submitted(self, event: PromptInput.Submitted) -> None:
        text = event.value.strip()
        self.query_one("#input", PromptInput).text = ""
        if not text:
            return
        if text.startswith("/"):
            await self._command(text)
            return
        if self._run_task and not self._run_task.done():
            self.tl.agent.steer(text)  # type: ignore[union-attr]
            self.log_line(
                f"[bold {MINT}]↳ steering:[/] [{MINT}]{text}[/] "
                f"[{GREY}](applied at the next turn)[/]"
            )
            return
        await self._start_prompt(text)

    def _update_check(self) -> None:
        """Daily release check off the UI thread; a dim line when something newer exists."""
        import os

        if os.environ.get("TRENDLAB_NO_UPDATE_CHECK"):
            return
        try:
            from trendlab.update import daily_hint

            hint = daily_hint()
        except Exception:  # noqa: BLE001
            return
        if hint:
            self.call_from_thread(self.log_line, f"[{GREY}]{hint}[/]")

    async def _start_prompt(self, text: str) -> None:
        """Run ``text`` as a prompt (typed, or expanded from a custom slash command)."""
        self.history.add(text)
        if self._run_task and not self._run_task.done():
            self.tl.agent.steer(text)  # type: ignore[union-attr]
            self.log_line(f"[bold {MINT}]↳ steering:[/] [{MINT}]{text[:200]}[/]")
            return
        shown = text if len(text) <= 400 else text[:400] + " …"
        if self.tl.pending_images:
            shown += f"  [{GREY}]📎 {len(self.tl.pending_images)} image(s)[/]"
        self.log_line(f"[bold {MINT}]❯[/] [bold {MINT}]{shown}[/]")
        self._started_at = time.monotonic()
        self._run_task = asyncio.create_task(self._run(text))

    def _command_catalog(self) -> list[tuple[str, str]]:
        from trendlab.ui.commands import command_catalog

        return command_catalog(self.tl)

    @on(PromptInput.CommandMenuRequested)
    def _command_menu_requested(self, event: PromptInput.CommandMenuRequested) -> None:
        try:
            menu = self.query_one("#cmdmenu", CommandPicker)
        except Exception:  # noqa: BLE001
            return
        if event.query is not None:
            menu.set_commands(self._command_catalog())
        menu.update_query(event.query)

    @on(OptionList.OptionSelected, "#cmdmenu")
    def _command_selected(self, event: OptionList.OptionSelected) -> None:
        menu = self.query_one("#cmdmenu", CommandPicker)
        choice = menu.choice()
        box = self.query_one("#input", PromptInput)
        if choice:
            box.text = choice + " "
            box.move_cursor(box.document.end)
        menu.hide()
        box.focus()

    def on_resize(self) -> None:
        self._apply_layout()

    def _apply_layout(self) -> None:
        """Compact header on short terminals; hide the plan panel when empty or narrow."""
        try:
            header = self.query_one("#header", Static)
            plan = self.query_one("#plan", Static)
        except Exception:  # noqa: BLE001 — not mounted yet
            return
        header.set_class(self.size.height < 30, "compact")
        plan.display = bool(self.tl.plan.tasks) and self.size.width >= 100
        if self.tl.gateway is not None:  # resize events can arrive before the app has started
            self._refresh_header()

    @on(PromptInput.ImagePasted)
    def _image_pasted(self, event: PromptInput.ImagePasted) -> None:
        self.tl.pending_images.append(event.path)
        n = len(self.tl.pending_images)
        self.log_line(
            f"[bold {MINT}]📎 image attached[/] [{GREY}]{event.path.name} · goes with your next "
            f"prompt ({n} pending)[/]"
        )
        if not supports_vision(self.tl.config, self.tl.model_ref):
            from trendlab.providers.catalog import cheapest_vision_model

            vision = cheapest_vision_model(self.tl.config, self.tl.model_ref)
            if vision:
                self.log_line(
                    f"[{GREY}]{self.tl.model_ref} cannot see images — this prompt will use "
                    f"[{MINT}]{vision}[/] (cheapest vision model with a key), then switch back[/]"
                )
            else:
                self.log_line(
                    f"[#ffd21f]⚠ {self.tl.model_ref} cannot see images and no vision model has a "
                    f"key — trendlab secret set OPENAI_API_KEY or ANTHROPIC_API_KEY, or F5[/]"
                )
        self._refresh_status()

    @on(PromptInput.PickRequested)
    def _pick_requested(self, event: PromptInput.PickRequested) -> None:
        self.file_index.root = self.tl.project_root
        try:
            picker = self.query_one("#picker", FilePicker)
        except Exception:  # noqa: BLE001 — a modal is up, or the app is closing
            return
        picker.update_query(event.query)

    @on(OptionList.OptionSelected, "#picker")
    def _pick_selected(self, event: OptionList.OptionSelected) -> None:
        picker = self.query_one("#picker", FilePicker)
        choice = picker.choice()
        if choice:
            self.query_one("#input", PromptInput).accept_pick(choice)
        self.query_one("#input", PromptInput).focus()

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
        except asyncio.CancelledError:
            self._flush_stream()
            self._refresh_header()
            return
        except Exception as exc:  # noqa: BLE001 — the TUI must survive runtime errors
            self._flush_stream()
            self.log_line(f"[bold {RED}]error:[/] {exc}")
            return
        self._flush_stream()
        if result.text.strip():
            self.log_line(Markdown(result.text))
        self.log_line(run_footer(result))
        self._refresh_header()
        self._refresh_plan()
        if self._started_at and time.monotonic() - self._started_at > 8:
            self.bell()  # long task finished: a nudge if you are in another window

    def action_interrupt(self) -> None:
        """Esc: close an open picker, else stop the current step; the conversation stays."""
        if isinstance(self.screen, ModelPicker):  # app-level priority bindings run first
            self.screen.action_cancel()
            return
        if self._run_task and not self._run_task.done():
            self.tl.agent.cancel()  # type: ignore[union-attr]
            self._run_task.cancel()
            self.log_line(
                f"[#ffd21f]■ Interrupted[/] [{GREY}]— type to continue the same session[/]"
            )

    def action_cancel_or_quit(self) -> None:
        if self._run_task and not self._run_task.done():
            self.action_interrupt()
            return
        self.exit()

    def action_clear(self) -> None:
        self._clear_log()

    def action_external_editor(self) -> None:
        from trendlab.ui.editor import edit_in_external_editor

        box = self.query_one("#input", PromptInput)
        with self.suspend():
            edited = edit_in_external_editor(box.text)
        if edited is None:
            self.log_line("[#ffd21f]no editor found — set $EDITOR (e.g. export EDITOR=nano)[/]")
            return
        box.text = edited.rstrip("\n")
        box.move_cursor(box.document.end)
        box.focus()

    async def _open_model_picker(self, choices: list[ModelChoice]) -> None:
        def done(ref: str | None) -> None:
            if ref is None:
                self.log_line(f"[{GREY}]model unchanged · {self.tl.model_ref}[/]")
            elif self.commands.switch_model(ref):
                self.log_line(Text(self.console_out.export_text(clear=True).rstrip(), style=NEON))
            else:
                self.log_line(Text(self.console_out.export_text(clear=True).rstrip(), style=RED))
            self._refresh_header()
            self._refresh_status()
            self.query_one("#input", PromptInput).focus()

        self.push_screen(ModelPicker(choices), done)

    async def action_pick_model(self) -> None:
        from trendlab.providers.catalog import list_model_choices

        await self._open_model_picker(list_model_choices(self.tl.config, self.tl.model_ref))

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
