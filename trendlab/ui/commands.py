"""Slash commands shared by the REPL and the Textual TUI (spec §31)."""

from __future__ import annotations

import re
import shlex
from collections.abc import Awaitable, Callable

from rich.console import Console
from rich.table import Table

from trendlab.app import TrendLabApp
from trendlab.approvals.channels.base import ChannelError
from trendlab.approvals.models import ApprovalDecision, ApprovalError, ApprovalScope
from trendlab.config.loader import ConfigError
from trendlab.config.schema import PermissionMode
from trendlab.providers.base import ProviderError
from trendlab.providers.catalog import list_model_choices, resolve_model_query
from trendlab.telemetry.events import EventType
from trendlab.tools.git import run_git
from trendlab.ui.diff_view import render_diff
from trendlab.ui.theme import GREY

HELP = """\
/help                      Show this help
/status                    Model, project, mode, session, remote, cost
/mode <plan|ask|auto_edit|trusted|auto>     Change permission mode (auto = no prompts)
/permissions               Policy table + session/project rules
/plan | /tasks             Show the current task plan
/context                   Context budget and compaction status
/compact                   Compact older conversation into a structured summary
/cost                      Session cost by model        /cost-limit <usd>   Set a hard budget
/diff [file]               Changes made this session (or git diff of a file)
/git <status|diff|log>     Read-only git commands
/commit [msg]              Commit everything; the message is written from the diff when omitted
/pr [title] [--draft]      Push the branch and open a pull request with gh (body generated)
/issue <n>                 Pull a GitHub issue into the conversation; /pr adds "Closes #n"
/worktree start [name] | done | list | remove <name>   Work on a throwaway git worktree
/checkpoint                List checkpoints                /undo [id] [force]   Restore a checkpoint
/sessions | /resume <id|latest> | /new      Session management (resume switches in place)
/branch [label] [--keep N]  Fork this conversation into a child session and continue there
/tree                      Sessions of this project as a branch tree
/plan gate on|off          Ask for a go-ahead (terminal/phone/Telegram) before the first change
/export [path]             Write this session as Markdown (.trendlab/exports/)
/edit                      Compose the next prompt in $EDITOR (TUI: Ctrl+E); end a line with \\
                           to continue on the next line
/model                     Pick a model (↑↓ Enter · type to filter · TUI: F5) — conversation kept
/model <name>              Switch directly; partial names work (/model haiku, /model mini)
/models                    Configured providers/models
/review                    Run the reviewer sub-agent on the current diff
/init                      Detect project tooling and draft TRENDLAB.md
/memory [forget <n>|clear] Facts learned about this project (.trendlab/memory.md)
/remember <fact>           Add a fact to project memory by hand
/skills [use <name>]       Reusable instruction packs      /hooks    Configured hooks
/commands [new <name>|reload]   Your own slash commands from .trendlab/commands/*.md
/bg [logs <id> [n]|stop <id>]   Background processes started by the agent (dev servers, watchers)
@path                      In any prompt: attach a file (or folder listing); TUI: Tab picks a match
/mcp                       MCP servers and their tools
/paste                     Attach the clipboard image to your next prompt (WSL/Linux/macOS)
/image <path>              Attach an image file to your next prompt (or write @file.png)
/remote [status|enable|disable|url]   Phone approval server
/approvals [approve <id> [session]|deny <id>|history]   Pending approvals
/telegram [on|off|status]  Remote control from your Telegram chat: send tasks, steer, get reports
/mouse [on|off]            TUI: off (default) leaves the mouse to the terminal so drag-select and
                           copy work like any terminal; on gives the app wheel scroll + clicks (F4)
/clear                     Clear the screen               /quit     Exit
"""


_HELP_LINE = re.compile(r"^(/[a-z][\w-]*)(?:\s[^\s].*?)?\s{2,}(.+)$")


def command_catalog(app) -> list[tuple[str, str]]:
    """``(name, description)`` for the slash menu: built-ins from HELP plus custom commands."""
    seen: dict[str, str] = {}
    for line in HELP.splitlines():
        m = _HELP_LINE.match(line.rstrip())
        if m and m.group(1) not in seen:
            seen[m.group(1)] = m.group(2).strip()
    for extra in ("/mouse", "/telegram", "/branch", "/tree", "/bg", "/commands", "/stop"):
        seen.setdefault(extra, {"/stop": "Interrupt the running task"}.get(extra, ""))
    lib = getattr(app, "custom_commands", None)
    if lib is not None:
        for c in lib.list():
            seen.setdefault(f"/{c.name}", f"{c.description}  [{c.source}]")
    return sorted(seen.items())


class CommandRouter:
    def __init__(
        self,
        app: TrendLabApp,
        console: Console,
        *,
        quit_cb: Callable[[], None] | None = None,
        clear_cb: Callable[[], None] | None = None,
        prompt_cb: Callable[[str], Awaitable[None]] | None = None,
        model_picker_cb: Callable[[list], Awaitable[None]] | None = None,
        interactive_stdin: bool = True,
    ) -> None:
        self.app = app
        self.console = console
        self._quit_cb = quit_cb
        self._clear_cb = clear_cb
        self._prompt_cb = prompt_cb  # how this UI runs a prompt (custom commands expand to one)
        self._model_picker_cb = model_picker_cb  # a UI's own picker (the TUI modal)
        self._interactive_stdin = interactive_stdin  # False for remote surfaces: never read stdin
        self._handlers: dict[str, Callable[[list[str]], Awaitable[None]]] = {
            "/help": self._help,
            "/status": self._status,
            "/mode": self._mode,
            "/permissions": self._permissions,
            "/plan": self._plan,
            "/tasks": self._plan,
            "/context": self._context,
            "/compact": self._compact,
            "/cost": self._cost,
            "/cost-limit": self._cost_limit,
            "/diff": self._diff,
            "/git": self._git,
            "/commit": self._commit,
            "/pr": self._pr,
            "/issue": self._issue,
            "/worktree": self._worktree,
            "/checkpoint": self._checkpoint,
            "/undo": self._undo,
            "/sessions": self._sessions,
            "/resume": self._resume,
            "/new": self._new,
            "/branch": self._branch,
            "/tree": self._tree,
            "/export": self._export,
            "/model": self._model,
            "/models": self._models,
            "/provider": self._models,
            "/review": self._review,
            "/init": self._init,
            "/skills": self._skills,
            "/memory": self._memory,
            "/remember": self._remember,
            "/commands": self._commands,
            "/bg": self._bg,
            "/hooks": self._hooks,
            "/mcp": self._mcp,
            "/paste": self._paste,
            "/image": self._image,
            "/remote": self._remote,
            "/telegram": self._telegram,
            "/approvals": self._approvals,
            "/clear": self._clear,
            "/quit": self._quit,
            "/exit": self._quit,
        }

    def register(self, name: str, handler: Callable[[list[str]], Awaitable[None]]) -> None:
        """Let a UI add a command of its own (the TUI registers /mouse)."""
        self._handlers[name.lower()] = handler

    async def dispatch(self, text: str) -> None:
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = text.split()
        if not parts:
            return
        cmd, args = parts[0].lower(), parts[1:]
        handler = self._handlers.get(cmd)
        if handler is None:
            if await self._run_custom(cmd, text):
                return
            self.console.print(f"[red]unknown command {cmd}[/red] — try /help")
            return
        await handler(args)

    async def _run_custom(self, cmd: str, text: str) -> bool:
        lib = self.app.custom_commands
        if lib is None or lib.get(cmd) is None:
            return False
        arg_text = text.strip()[len(cmd) :].strip() if text.strip().lower().startswith(cmd) else ""
        prompt = lib.render(cmd, arg_text)
        if not prompt:
            return False
        self.app.events.emit(
            EventType.CUSTOM_COMMAND, session_id=self.app.session_id, command=cmd, chars=len(prompt)
        )
        if self._prompt_cb is None:
            self.console.print(f"[red]{cmd} is a prompt command; this UI cannot run prompts[/red]")
            return True
        self.console.print(f"[{GREY}]{cmd} → prompt ({len(prompt)} chars)[/]")
        await self._prompt_cb(prompt)
        return True

    # -- basics -------------------------------------------------------------------------------
    async def _help(self, args: list[str]) -> None:
        self.console.print(HELP)

    async def _status(self, args: list[str]) -> None:
        app = self.app
        s = app.remote_status()
        t = Table.grid(padding=(0, 2))
        t.add_row("Model", f"{app.model_ref} [{app.privacy_label()}]")
        t.add_row("Project", str(app.project_root))
        t.add_row("Session", app.session_id + (" (resumed)" if app.resumed else ""))
        t.add_row("Mode", app.engine.mode.value)
        t.add_row("State", app.agent.state.state.value if app.agent else "-")
        if app.costs:
            t.add_row(
                "Cost",
                f"${app.costs.total_usd:.3f} · {app.costs.model_calls} calls · "
                f"{app.costs.total_input_tokens} in / {app.costs.total_output_tokens} out",
            )
        if app.context:
            st = app.context.status()
            t.add_row(
                "Context",
                f"~{st['estimated_tokens']} / {st['context_window']} tokens · "
                f"{st['messages']} msgs · {st['compactions']} compactions",
            )
        t.add_row("Plan", f"{len(app.plan.open)} open / {len(app.plan.tasks)} tasks")
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
            self.console.print("[red]modes: plan, ask, auto_edit, trusted, auto[/red]")
            return
        if self.app.engine.unsafe:
            self.console.print(
                "[bold black on #ffd21f] AUTO [/bold black on #ffd21f] [#ffd21f]approval prompts "
                "are off; elevated commands and outside-project paths stay off-limits; "
                "/mode ask turns prompts on[/#ffd21f]"
            )
        else:
            self.console.print(f"mode set to [bold]{self.app.engine.mode.value}[/bold]")

    async def _permissions(self, args: list[str]) -> None:
        title = f"Permissions — mode {self.app.engine.mode.value}"
        if self.app.engine.unsafe:
            title += "  [bold black on #ffd21f] AUTO [/bold black on #ffd21f]"
        t = Table(title=title)
        t.add_column("Operation")
        t.add_column("Policy")
        for cat, dec in self.app.engine.policy_table().items():
            color = {"allow": "green", "ask": "yellow", "deny": "red"}[dec.value]
            t.add_row(cat.value, f"[{color}]{dec.value.upper()}[/{color}]")
        self.console.print(t)
        rules = self.app.engine.session_rules()
        if rules:
            self.console.print(
                "Session rules: " + ", ".join(f"{k}={v.value}" for k, v in rules.items())
            )
        if self.app.engine.project_rules and self.app.engine.project_rules.all():
            self.console.print(
                "Project rules: "
                + ", ".join(
                    f"{k}={v.value}" for k, v in self.app.engine.project_rules.all().items()
                )
            )

    # -- plan / context / cost -------------------------------------------------------------------
    async def _plan(self, args: list[str]) -> None:
        if args and args[0] == "gate":
            gate = self.app.plan_gate
            if gate is None:
                self.console.print("[red]plan gate not available[/red]")
                return
            if len(args) > 1 and args[1] in {"on", "off"}:
                gate.config.enabled = args[1] == "on"
            st = gate.status()
            state = "ON" if st["enabled"] else "off"
            run = (
                "approved"
                if st["approved"]
                else ("rejected" if st["rejected"] else "not asked yet")
            )
            self.console.print(
                f"plan gate [bold]{state}[/bold] · timeout {st['timeout_minutes']} min · "
                f"this run: {run}"
            )
            return
        self.console.print(self.app.plan.render())
        for t in self.app.plan.tasks:
            for e in t.evidence:
                self.console.print(f"    [dim]{t.id} evidence: {e}[/dim]")

    async def _context(self, args: list[str]) -> None:
        if not self.app.context:
            return
        st = self.app.context.status()
        t = Table.grid(padding=(0, 2))
        for k, v in st.items():
            t.add_row(k.replace("_", " "), str(v))
        self.console.print(t)

    async def _compact(self, args: list[str]) -> None:
        if not self.app.context:
            return
        rec = await self.app.context.compact(force=True)
        if rec is None:
            self.console.print("[dim]nothing to compact[/dim]")
            return
        self.console.print(
            f"[green]Compacted {rec.messages_compacted} messages[/green] "
            f"({rec.tokens_before} → {rec.tokens_after} est. tokens, {rec.source})"
        )
        self.console.print(rec.summary[:1500])

    async def _cost(self, args: list[str]) -> None:
        costs = self.app.costs
        if not costs:
            return
        t = Table(title="Session cost")
        for col in ("model", "calls", "input", "output", "usd"):
            t.add_column(col)
        for ref, d in costs.by_model().items():
            t.add_row(
                ref + (" (local)" if d["local"] else ""),
                str(d["calls"]),
                str(d["input"]),
                str(d["output"]),
                f"${d['usd']:.4f}",
            )
        t.add_row(
            "[bold]total[/bold]",
            str(costs.model_calls),
            str(costs.total_input_tokens),
            str(costs.total_output_tokens),
            f"[bold]${costs.total_usd:.4f}[/bold]",
        )
        self.console.print(t)
        limit = self.app.config.limits.max_cost_usd
        self.console.print(
            f"Budget: {f'${limit:.2f}' if limit else 'none'} · "
            f"pricing entries: {len(self.app.config.pricing)}"
        )

    async def _cost_limit(self, args: list[str]) -> None:
        if not args:
            self.console.print(f"cost limit: {self.app.config.limits.max_cost_usd}")
            return
        try:
            self.app.config.limits.max_cost_usd = float(args[0])
        except ValueError:
            self.console.print("[red]usage: /cost-limit <usd>[/red]")
            return
        self.console.print(f"cost limit set to ${self.app.config.limits.max_cost_usd:.2f}")

    # -- diff / git -------------------------------------------------------------------------------
    async def _diff(self, args: list[str]) -> None:
        tools = self.app.tools
        if args:
            code, out = await run_git(self.app.project_root, "diff", "--", args[0])
            self.console.print(
                render_diff(out, title=f"git diff {args[0]}")
                if out.strip()
                else "[dim]no changes[/dim]"
            )
            return
        if not tools or not tools.changed_files:
            self.console.print("[dim]no files changed this session[/dim]")
            return
        for path, diffs in tools.changed_files.items():
            for d in diffs:
                if d:
                    self.console.print(render_diff(d, title=path))

    async def _git(self, args: list[str]) -> None:
        sub = args[0] if args else "status"
        if sub not in {"status", "diff", "log", "show", "branch"}:
            self.console.print("[red]usage: /git <status|diff|log|show|branch>[/red]")
            return
        extra = args[1:] if sub != "log" else ["--oneline", "-n", "15", *args[1:]]
        code, out = await run_git(self.app.project_root, sub, *extra)
        self.console.print(
            render_diff(out) if sub in {"diff", "show"} and out.strip() else out or "(empty)"
        )

    async def _commit(self, args: list[str]) -> None:
        from trendlab.orchestration.gitflow import commit

        message = " ".join(args) if args else None
        if message is None:
            self.console.print(f"[{GREY}]generating a commit message from the diff…[/]")
        res = await commit(self.app, message)
        if not res["ok"]:
            self.console.print(f"[red]{res['error']}[/red]")
            return
        self.console.print(f"[ok]committed {res['head']}[/ok]")
        self.console.print(res["message"])

    async def _pr(self, args: list[str]) -> None:
        from trendlab.orchestration.gitflow import create_pr

        draft = "--draft" in args
        title = " ".join(a for a in args if a != "--draft") or None
        self.console.print(f"[{GREY}]pushing branch and opening a pull request…[/]")
        res = await create_pr(self.app, title, draft=draft)
        if not res["ok"]:
            self.console.print(f"[red]{res['error']}[/red]")
            return
        self.console.print(
            f"[ok]pull request opened[/ok] {res['url']}  ({res['branch']} → {res['base']})"
        )

    async def _issue(self, args: list[str]) -> None:
        from trendlab.orchestration.gitflow import load_issue

        if not args or not args[0].lstrip("#").isdigit():
            self.console.print("[red]usage: /issue <number>[/red]")
            return
        res = await load_issue(self.app, int(args[0].lstrip("#")))
        if not res["ok"]:
            self.console.print(f"[red]{res['error']}[/red]")
            return
        self.console.print(
            f"[ok]loaded issue #{res['issue']['number']}[/ok] {res['issue'].get('title', '')}"
        )
        self.console.print(
            f"[{GREY}]its text is now in the conversation; /pr will add 'Closes #N'[/]"
        )

    async def _worktree(self, args: list[str]) -> None:
        from trendlab.orchestration.gitflow import WorktreeManager

        wm = WorktreeManager(self.app.main_project_root)
        sub = args[0] if args else "list"
        if sub == "start":
            name = " ".join(args[1:]) if len(args) > 1 else self.app.session_id
            try:
                path = await wm.create(name)
            except RuntimeError as exc:
                self.console.print(f"[red]{exc}[/red]")
                return
            self.app.switch_project_root(path)
            self.console.print(f"[ok]working in worktree[/ok] {path} (branch trendlab/{name})")
        elif sub == "done":
            self.app.switch_project_root(self.app.main_project_root)
            self.console.print(
                f"[ok]back to[/ok] {self.app.main_project_root}  "
                f"[{GREY}](worktree kept; /worktree remove <name> to delete)[/]"
            )
        elif sub == "remove" and len(args) > 1:
            try:
                path = await wm.remove(args[1], force="--force" in args)
            except RuntimeError as exc:
                self.console.print(f"[red]{exc}[/red]")
                return
            if str(self.app.project_root) == path:
                self.app.switch_project_root(self.app.main_project_root)
            self.console.print(f"[ok]removed[/ok] {path}")
        else:
            rows = await wm.list()
            t = Table(title="Worktrees")
            t.add_column("path")
            t.add_column("branch")
            for r in rows:
                mark = " ◀" if str(self.app.project_root) == r["path"] else ""
                t.add_row(r["path"] + mark, r.get("branch", "-"))
            self.console.print(t)

    # -- checkpoints / sessions --------------------------------------------------------------------
    async def _checkpoint(self, args: list[str]) -> None:
        cps = self.app.checkpoints.list() if self.app.checkpoints else []
        if not cps:
            self.console.print("[dim]no checkpoints[/dim]")
            return
        t = Table(title="Checkpoints")
        for col in ("id", "created", "label", "files", "git head"):
            t.add_column(col)
        for cp in cps:
            t.add_row(
                cp["id"],
                cp["created_at"][:19],
                cp["label"] or "-",
                ", ".join(e["path"] for e in cp["files"])[:60],
                (cp["git_head"] or "-")[:8],
            )
        self.console.print(t)

    async def _undo(self, args: list[str]) -> None:
        if not self.app.checkpoints:
            return
        force = "force" in args
        cid = next((a for a in args if a != "force"), None)
        res = self.app.checkpoints.undo(cid, force=force)
        if res["ok"]:
            self.console.print(
                f"[green]Restored checkpoint {res['checkpoint']}[/green]: "
                f"{', '.join(res['restored']) or '-'}"
                + (f" · removed {', '.join(res['removed'])}" if res["removed"] else "")
            )
            if self.app.tools:
                for f in res["restored"] + res["removed"]:
                    self.app.tools.changed_files.pop(f, None)
        else:
            self.console.print(
                f"[red]{res['error']}[/red]"
                + (f": {', '.join(res['conflicts'])}" if res.get("conflicts") else "")
            )

    async def _sessions(self, args: list[str]) -> None:
        rows = self.app.store.sessions(str(self.app.project_root)) if self.app.store else []
        t = Table(title="Sessions (this project)")
        for col in ("id", "updated", "status", "model", "msgs", "cost", "parent"):
            t.add_column(col)
        for r in rows:
            usage = self.app.store.usage(r["id"])  # type: ignore[union-attr]
            mark = " ◀" if r["id"] == self.app.session_id else ""
            t.add_row(
                r["id"] + mark,
                r["updated_at"][:19],
                r["status"],
                r["model"] or "-",
                str(len(self.app.store.messages(r["id"]))),
                f"${usage['cost_usd']:.3f}",
                (r.get("parent_id") or "-") + (f" “{r['label']}”" if r.get("label") else ""),
            )  # type: ignore[union-attr]
        self.console.print(t)
        self.console.print(
            "[dim]Resume with: trendlab --resume <id>  (or /resume <id> to switch now)[/dim]"
        )

    async def _export(self, args: list[str]) -> None:
        from pathlib import Path

        target = Path(args[0]).expanduser() if args else None
        path = self.app.export_transcript(target)
        self.console.print(f"[ok]Exported[/ok] {path}")

    async def _resume(self, args: list[str]) -> None:
        if not args:
            self.console.print("[red]usage: /resume <session-id|latest>[/red]")
            return
        if (
            self.app.agent
            and not self.app.agent.state.terminal
            and self.app.agent.state.state.value != "IDLE"
        ):
            self.console.print(
                "[red]a task is running; cancel it (Ctrl+C) before switching sessions[/red]"
            )
            return
        try:
            sid = self.app.switch_session(args[0])
        except KeyError:
            self.console.print(
                f"[red]no session {args[0]!r} for this project (see /sessions)[/red]"
            )
            return
        n = len(self.app.context.messages) if self.app.context else 0
        self.console.print(
            f"[ok]Resumed session {sid}[/ok] · {n} messages · model {self.app.model_ref}"
        )

    async def _branch(self, args: list[str]) -> None:
        if (
            self.app.agent
            and not self.app.agent.state.terminal
            and self.app.agent.state.state.value != "IDLE"
        ):
            self.console.print("[red]a task is running; wait or cancel before branching[/red]")
            return
        keep = None
        words = list(args)
        if "--keep" in words:
            i = words.index("--keep")
            if i + 1 < len(words) and words[i + 1].isdigit():
                keep = int(words[i + 1])
            del words[i : i + 2]
        label = " ".join(words) or None
        parent = self.app.session_id
        sid = self.app.branch_session(label, keep_last=keep)
        n = len(self.app.context.messages) if self.app.context else 0
        self.console.print(
            f"[green]Branched[/green] {parent} → [bold]{sid}[/bold]"
            + (f" “{label}”" if label else "")
            + f" · {n} messages carried over · /resume {parent} returns to the parent"
        )

    async def _tree(self, args: list[str]) -> None:
        store = self.app.store
        if store is None:
            return
        rows = store.session_tree(str(self.app.project_root))
        if not rows:
            self.console.print("[dim]no sessions[/dim]")
            return
        t = Table(title="Session tree (this project)")
        for col in ("session", "label", "updated", "status", "msgs", "branched at"):
            t.add_column(col)
        for depth, r in rows:
            mark = " ◀" if r["id"] == self.app.session_id else ""
            prefix = ("   " * (depth - 1) + "└─ ") if depth else ""
            t.add_row(
                f"{prefix}{r['id']}{mark}",
                r.get("label") or "-",
                r["updated_at"][:19],
                r["status"],
                str(len(store.messages(r["id"]))),
                f"msg {r['branch_point']}" if r.get("branch_point") is not None else "-",
            )
        self.console.print(t)

    async def _new(self, args: list[str]) -> None:
        app = self.app
        if app.context:
            app.context.messages.clear()
            app.context.summary = None
        app.plan.tasks.clear()
        if app.tools:
            app.tools.changed_files.clear()
            app.tools.validation_runs.clear()
        if app.store:
            app.store.set_session_status(app.session_id, "closed")
            from trendlab.app import machine_name

            app.session_id = app.store.create_session(
                str(app.project_root), machine_name(app.config), app.model_ref
            )
            app.approvals.session_id = app.session_id  # type: ignore[union-attr]
            app.agent.session_id = app.session_id  # type: ignore[union-attr]
            app.context.session_id = app.session_id  # type: ignore[union-attr]
            app.tools.ctx.session_id = app.session_id  # type: ignore[union-attr]
        self.console.print(f"[green]New session {app.session_id}[/green]")

    # -- models ------------------------------------------------------------------------------------
    async def _model(self, args: list[str]) -> None:
        choices = list_model_choices(self.app.config, self.app.model_ref)
        if not args:
            if self._model_picker_cb is not None:
                await self._model_picker_cb(choices)
                return
            await self._pick_model_plain(choices)
            return
        query = " ".join(args)
        provider = query.split(":", 1)[0] if ":" in query else ""
        if provider and provider in self.app.config.providers:
            self.switch_model(query)  # any model name of a configured provider is valid
            return
        hit = resolve_model_query(choices, query)
        if isinstance(hit, list):
            if not hit:
                self.console.print(
                    f"[red]no model matches {' '.join(args)!r}[/red] — /model lists the choices"
                )
            else:
                self.console.print("[yellow]ambiguous — did you mean:[/yellow]")
                for c in hit:
                    self.console.print(f"  {c.ref}  [{GREY}]{c.note}[/]")
            return
        self.switch_model(hit.ref)

    def switch_model(self, ref: str) -> bool:
        """Switch and print a one-line receipt; returns False when the provider rejects it."""
        before = self.app.model_ref
        try:
            self.app.switch_model(ref)
        except ProviderError as exc:
            self.console.print(f"[red]{exc}[/red]")
            return False
        choice = next((c for c in list_model_choices(self.app.config, ref) if c.ref == ref), None)
        detail = f" · {choice.ctx_label} · {choice.price_label}" if choice else ""
        same = " (already active)" if before == ref else ""
        self.console.print(
            f"[bold]model →[/bold] [bold green]{ref}[/bold green] "
            f"[{GREY}][{self.app.privacy_label()}]{detail} · conversation kept{same}[/]"
        )
        if choice is not None and "experimental" in choice.note:
            self.console.print(
                "[#ffd21f]⚠ small local model: fine for questions, unreliable for multi-step "
                "tool use — switch back to deepseek:deepseek-flash for real tasks[/]"
            )
        return True

    async def _pick_model_plain(self, choices: list) -> None:
        """Numbered list for the plain REPL; Enter keeps the current model."""
        if not self._interactive_stdin:  # remote surface (Telegram): plain lines, no table
            self.console.print("Models (◀ = current):")
            for c in choices:
                mark = " ◀" if c.current else ""
                self.console.print(
                    f"• {c.ref}{mark} · {c.ctx_label} · {c.price_label} · {c.status_label}"
                )
            self.console.print(
                "Switch with: /model <name>  (partial names work, e.g. /model flash)"
            )
            return
        t = Table(title="Models — type a number (Enter keeps the current one)")
        for col in ("#", "model", "where", "context", "price", "key", "note"):
            t.add_column(col)
        for i, c in enumerate(choices, 1):
            mark = " ◀" if c.current else ""
            t.add_row(
                str(i),
                c.ref + mark,
                "local" if c.local else "remote",
                c.ctx_label,
                c.price_label,
                c.status_label,
                c.note,
            )
        self.console.print(t)
        reader = getattr(self.app, "console_input", None)
        if reader is None or not self._interactive_stdin:
            self.console.print(
                "Switch with: /model <name>  (partial names work, e.g. /model flash)"
            )
            return
        self.console.print("[bold]model #❯[/bold] ", end="")
        line = await reader.readline()
        text = (line or "").strip()
        if not text:
            return
        if text.isdigit() and 1 <= int(text) <= len(choices):
            self.switch_model(choices[int(text) - 1].ref)
            return
        hit = resolve_model_query(choices, text)
        if isinstance(hit, list):
            self.console.print(f"[red]no such model: {text}[/red]")
            return
        self.switch_model(hit.ref)

    async def _models(self, args: list[str]) -> None:
        t = Table(title="Providers")
        for col in ("provider", "type", "base_url", "key env", "tool calling"):
            t.add_column(col)
        for name, p in self.app.config.providers.items():
            t.add_row(name, p.type, p.base_url, p.api_key_env or "-", p.tool_calling)
        self.console.print(t)
        if self.app.config.models:
            t2 = Table(title="Model registry")
            for col in ("model", "context", "tools", "local"):
                t2.add_column(col)
            for ref, m in self.app.config.models.items():
                t2.add_row(
                    ref,
                    str(m.context_window or "-"),
                    "yes" if m.supports_tools else "structured",
                    str(m.local) if m.local is not None else "auto",
                )
            self.console.print(t2)
        if self.app.config.routing:
            self.console.print(
                "Routing: " + ", ".join(f"{k}→{v}" for k, v in self.app.config.routing.items())
            )

    # -- sub-agents / extensions (implemented in later modules; degrade gracefully) ----------------
    async def _review(self, args: list[str]) -> None:
        try:
            from trendlab.orchestration.review import run_review
        except ImportError:
            self.console.print("[red]review agent not available[/red]")
            return
        report = await run_review(self.app)
        self.console.print(report)

    async def _init(self, args: list[str]) -> None:
        try:
            from trendlab.extensions.init import draft_instructions
        except ImportError:
            self.console.print("[red]/init not available[/red]")
            return
        draft = draft_instructions(
            self.app.project_root, self.app.tools.ctx.validation_commands or {}
        )  # type: ignore[union-attr]
        target = self.app.project_root / "TRENDLAB.md"
        self.console.print(draft)
        if target.exists() and "force" not in args:
            self.console.print(
                f"[yellow]{target.name} exists; run /init force to overwrite[/yellow]"
            )
            return
        if "save" in args or "force" in args:
            target.write_text(draft, encoding="utf-8")
            self.console.print(f"[green]wrote {target.name}[/green]")
        else:
            self.console.print("[dim]Review above, then /init save to write TRENDLAB.md[/dim]")

    async def _skills(self, args: list[str]) -> None:
        lib = self.app.skills
        if lib is None:
            self.console.print("[red]skills not available[/red]")
            return
        if args and args[0] == "use" and len(args) > 1:
            skill = lib.get(args[1])
            if skill is None:
                self.console.print(f"[red]unknown skill {args[1]}[/red]")
                return
            lib.activate(skill.name)
            self.app.refresh_system_prompt()
            self.console.print(f"[green]skill {skill.name} active[/green]: {skill.description}")
            return
        t = Table(title="Skills")
        for col in ("name", "active", "description", "source"):
            t.add_column(col)
        for sk in lib.list():
            t.add_row(
                sk.name,
                "✓" if sk.name in lib.active else "",
                sk.description[:60],
                str(sk.path.parent),
            )
        self.console.print(
            t if lib.list() else "[dim]no skills found (~/.trendlab/skills/<name>/SKILL.md)[/dim]"
        )

    async def _memory(self, args: list[str]) -> None:
        mem = self.app.memory
        if mem is None:
            self.console.print("[dim]project memory is disabled ([memory] enabled = false)[/dim]")
            return
        if args and args[0] == "clear":
            n = mem.clear()
            self.app.refresh_system_prompt()
            self.console.print(f"[green]forgot {n} fact(s)[/green]")
            return
        if args and args[0] == "forget" and len(args) > 1 and args[1].isdigit():
            gone = mem.forget(int(args[1]))
            self.app.refresh_system_prompt()
            self.console.print(
                f"[green]forgot:[/green] {gone}" if gone else "[red]no such entry[/red]"
            )
            return
        if not mem.facts:
            self.console.print(
                f"[dim]nothing remembered yet — facts are learned after runs that edit, validate "
                f"or get steered; /remember <fact> adds one ({mem.path})[/dim]"
            )
            return
        self.console.print(f"[bold]Project memory[/bold] [{GREY}]{mem.path}[/]")
        for i, (when, fact) in enumerate(mem.entries, 1):
            self.console.print(f"  {i:>2}. {fact} [{GREY}]{when}[/]")
        self.console.print(f"[{GREY}]/memory forget <n> · /memory clear · /remember <fact>[/]")

    async def _remember(self, args: list[str]) -> None:
        mem = self.app.memory
        text = " ".join(args).strip()
        if mem is None or not text:
            self.console.print("[red]usage: /remember <fact about this project>[/red]")
            return
        if mem.add(text):
            self.app.refresh_system_prompt()
            self.console.print(f"[green]remembered:[/green] {text}")
        else:
            self.console.print("[dim]already remembered (or too short)[/dim]")

    async def _commands(self, args: list[str]) -> None:
        lib = self.app.custom_commands
        if lib is None:
            self.console.print("[red]custom commands not available[/red]")
            return
        if args and args[0] == "reload":
            lib.reload()
        elif args and args[0] == "new" and len(args) > 1:
            try:
                path = lib.scaffold(args[1], project=not (len(args) > 2 and args[2] == "--global"))
            except ValueError as exc:
                self.console.print(f"[red]{exc}[/red]")
                return
            self.console.print(f"[green]wrote {path}[/green] — edit it, then type /{args[1]}")
            return
        cmds = lib.list()
        if not cmds:
            self.console.print(
                f"[dim]no custom commands — /commands new <name> writes "
                f"{lib.project_dir.relative_to(self.app.project_root)}/<name>.md[/dim]"
            )
            return
        t = Table(title="Custom commands")
        for col in ("command", "description", "source", "file"):
            t.add_column(col)
        for c in cmds:
            t.add_row(f"/{c.name}", c.description, c.source, str(c.path))
        self.console.print(t)

    async def _bg(self, args: list[str]) -> None:
        mgr = self.app.background
        if mgr is None:
            self.console.print("[red]background processes not available[/red]")
            return
        if args and args[0] == "stop" and len(args) > 1:
            st = await mgr.stop(args[1])
            self.console.print(
                f"[green]stopped {args[1]}[/green]" if st else f"[red]no process {args[1]}[/red]"
            )
            return
        if args and args[0] == "logs" and len(args) > 1:
            bp = mgr.get(args[1])
            if bp is None:
                self.console.print(f"[red]no process {args[1]}[/red]")
                return
            n = int(args[2]) if len(args) > 2 and args[2].isdigit() else 40
            self.console.print(bp.tail(n) or "[dim](no output yet)[/dim]")
            return
        procs = mgr.list()
        if not procs:
            self.console.print("[dim]no background processes[/dim]")
            return
        t = Table(title="Background processes")
        for col in ("id", "name", "state", "uptime", "command", "log"):
            t.add_column(col)
        for p in procs:
            st = p.status()
            t.add_row(
                st["id"],
                st["name"],
                "running" if st["running"] else f"exit {st['exit_code']}",
                f"{st['uptime_s']}s",
                st["command"][:50],
                str(p.log_path.name),
            )
        self.console.print(t)

    async def _hooks(self, args: list[str]) -> None:
        hooks = self.app.config.hooks
        if not hooks:
            self.console.print("[dim]no hooks configured ([[hooks]] in config.toml)[/dim]")
            return
        t = Table(title="Hooks")
        for col in ("event", "command", "timeout", "blocking"):
            t.add_column(col)
        for h in hooks:
            t.add_row(h.event, h.command, f"{h.timeout_seconds}s", "yes" if h.blocking else "no")
        self.console.print(t)

    async def _mcp(self, args: list[str]) -> None:
        if self.app.mcp is None:
            self.console.print(
                "[dim]no MCP servers configured ([mcp.servers.<name>] in config.toml)[/dim]"
            )
            return
        t = Table(title="MCP servers")
        for col in ("server", "status", "tools"):
            t.add_column(col)
        for name, info in self.app.mcp.status().items():
            t.add_row(name, info["status"], ", ".join(info["tools"]) or "-")
        self.console.print(t)

    async def _paste(self, args: list[str]) -> None:
        from trendlab.ui.attachments import grab_clipboard_image

        path = grab_clipboard_image()
        if path is None:
            self.console.print(
                "[warning]no image on the clipboard (or no clipboard tool found)[/warning]"
            )
            return
        self.app.pending_images.append(path)
        self.console.print(f"[ok]attached[/ok] {path.name} — it goes with your next prompt")

    async def _image(self, args: list[str]) -> None:
        from pathlib import Path

        from trendlab.ui.attachments import IMAGE_EXTS

        if not args:
            self.console.print("[red]usage: /image <path>[/red]")
            return
        path = Path(args[0]).expanduser()
        if not path.is_absolute():
            path = self.app.project_root / path
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTS:
            self.console.print(f"[red]not an image file: {args[0]}[/red]")
            return
        self.app.pending_images.append(path.resolve())
        self.console.print(f"[ok]attached[/ok] {path.name} — it goes with your next prompt")

    # -- remote / approvals ------------------------------------------------------------------------
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

    async def _telegram(self, args: list[str]) -> None:
        app = self.app
        if args and args[0] == "on":
            try:
                await app.enable_telegram_bridge()
            except Exception as exc:  # noqa: BLE001
                self.console.print(f"[red]{exc}[/red]")
                return
        elif args and args[0] == "off":
            await app.disable_telegram_bridge()
        br = app.telegram_bridge
        if br is None or not br.running:
            self.console.print(
                "[dim]Telegram remote control off — /telegram on (needs "
                "notifications.telegram.chat_id + secret TRENDLAB_TELEGRAM_BOT_TOKEN)[/dim]"
            )
            return
        st = br.status()
        self.console.print(
            f"[green]Telegram remote control ON[/green] · chat {st['chat_id']} · "
            f"{st['messages_in']} in / {st['messages_out']} out · up {st['uptime_s']}s"
            + (f" · [red]last error: {st['last_error']}[/red]" if st["last_error"] else "")
        )

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
                self.console.print(f"[red]usage: /approvals {sub} <id> [session|project][/red]")
                return
            match = [r for r in mgr.pending() if r.approval_id.startswith(args[1])]
            if len(match) != 1:
                self.console.print("[red]no unique pending approval with that id[/red]")
                return
            scope = ApprovalScope.ONCE
            if len(args) > 2 and args[2] in {"session", "project"}:
                scope = ApprovalScope(args[2])
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
                "[red]usage: /approvals [list|approve <id> [session|project]|deny <id>|history]"
                "[/red]"
            )

    async def _clear(self, args: list[str]) -> None:
        if self._clear_cb:
            self._clear_cb()
        else:
            self.console.clear()

    async def _quit(self, args: list[str]) -> None:
        if self._quit_cb:
            self._quit_cb()
