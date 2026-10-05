"""Slash commands shared by the REPL and the Textual TUI (spec §31)."""

from __future__ import annotations

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
from trendlab.tools.git import run_git
from trendlab.ui.diff_view import render_diff
from trendlab.ui.theme import GREY

HELP = """\
/help                      Show this help
/status                    Model, project, mode, session, remote, cost
/mode <plan|ask|auto_edit|trusted|unsafe>   Change permission mode (unsafe = no prompts)
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
/export [path]             Write this session as Markdown (.trendlab/exports/)
/edit                      Compose the next prompt in $EDITOR (TUI: Ctrl+E); end a line with \\
                           to continue on the next line
/model <provider:model>    Hot-switch the model            /models   Configured providers/models
/review                    Run the reviewer sub-agent on the current diff
/init                      Detect project tooling and draft TRENDLAB.md
/skills [use <name>]       Reusable instruction packs      /hooks    Configured hooks
/mcp                       MCP servers and their tools
/paste                     Attach the clipboard image to your next prompt (WSL/Linux/macOS)
/image <path>              Attach an image file to your next prompt (or write @file.png)
/remote [status|enable|disable|url]   Phone approval server
/approvals [approve <id> [session]|deny <id>|history]   Pending approvals
/clear                     Clear the screen               /quit     Exit
"""


class CommandRouter:
    def __init__(
        self,
        app: TrendLabApp,
        console: Console,
        *,
        quit_cb: Callable[[], None] | None = None,
        clear_cb: Callable[[], None] | None = None,
    ) -> None:
        self.app = app
        self.console = console
        self._quit_cb = quit_cb
        self._clear_cb = clear_cb
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
            "/export": self._export,
            "/model": self._model,
            "/models": self._models,
            "/provider": self._models,
            "/review": self._review,
            "/init": self._init,
            "/skills": self._skills,
            "/hooks": self._hooks,
            "/mcp": self._mcp,
            "/paste": self._paste,
            "/image": self._image,
            "/remote": self._remote,
            "/approvals": self._approvals,
            "/clear": self._clear,
            "/quit": self._quit,
            "/exit": self._quit,
        }

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
            self.console.print(f"[red]unknown command {cmd}[/red] — try /help")
            return
        await handler(args)

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
            self.console.print("[red]modes: plan, ask, auto_edit, trusted, unsafe[/red]")
            return
        if self.app.engine.unsafe:
            self.console.print(
                "[bold white on red] UNSAFE [/bold white on red] [red]approval prompts are off; "
                "sudo and outside-project paths stay denied; /mode ask turns prompts on[/red]"
            )
        else:
            self.console.print(f"mode set to [bold]{self.app.engine.mode.value}[/bold]")

    async def _permissions(self, args: list[str]) -> None:
        title = f"Permissions — mode {self.app.engine.mode.value}"
        if self.app.engine.unsafe:
            title += "  [bold white on red] UNSAFE [/bold white on red]"
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
        for col in ("id", "updated", "status", "model", "msgs", "cost"):
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
        if not args:
            self.console.print(f"model: {self.app.model_ref} [{self.app.privacy_label()}]")
            return
        try:
            self.app.switch_model(args[0])
        except ProviderError as exc:
            self.console.print(f"[red]{exc}[/red]")
            return
        self.console.print(f"model switched to [bold]{args[0]}[/bold] [{self.app.privacy_label()}]")

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
            if self.app.context:
                self.app.context.system_prompt = lib.apply(self.app.context.system_prompt)
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
