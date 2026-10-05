"""Composition root: config → store → events → permissions → approvals → gateway → context →
tools → agent. UI layers (REPL, Textual TUI) talk to ``TrendLabApp`` and contain no agent logic."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.console import Console

from trendlab import PRODUCT_NAME
from trendlab.agent.prompt import build_system_prompt
from trendlab.agent.runtime import AgentRuntime, RunResult
from trendlab.agent.tasks import Plan
from trendlab.approvals.channels.local import LocalTerminalChannel
from trendlab.approvals.channels.web import WebApprovalChannel
from trendlab.approvals.manager import ApprovalManager
from trendlab.approvals.notifications.base import Notification, NotificationProvider
from trendlab.approvals.notifications.registry import create_notification_provider
from trendlab.approvals.tokens import load_or_create_token, pairing_url
from trendlab.config.loader import trendlab_home
from trendlab.config.schema import AppConfig, PermissionMode
from trendlab.context.ignore import IgnoreRules
from trendlab.context.manager import ContextManager
from trendlab.context.repository_map import RepositoryMap
from trendlab.context.validation import validation_commands
from trendlab.orchestration.subagents import SubAgentRunner, delegate_tool_factory
from trendlab.permissions.engine import PermissionEngine
from trendlab.permissions.rules import ProjectRules
from trendlab.providers.base import ModelProvider
from trendlab.providers.gateway import ModelGateway
from trendlab.providers.registry import model_info, resolve_role
from trendlab.sessions.checkpoints import CheckpointManager
from trendlab.sessions.store import SessionStore
from trendlab.telemetry.costs import CostTracker
from trendlab.telemetry.events import Event, EventBus, EventType, JsonlEventSink
from trendlab.tools.ask_user import AskUserTool
from trendlab.tools.base import ToolContext
from trendlab.tools.registry import default_registry
from trendlab.tools.runtime import ToolRuntime
from trendlab.tools.task_tool import TaskTool
from trendlab.ui.attachments import build_user_content, text_of
from trendlab.ui.console import ConsoleInput


def machine_name(config: AppConfig) -> str:
    return config.remote_approval.machine_name or socket.gethostname()


class TrendLabApp:
    def __init__(
        self,
        project_root: Path,
        config: AppConfig,
        *,
        model_ref: str | None = None,
        provider: ModelProvider | None = None,
        console: Console | None = None,
        console_input: ConsoleInput | None = None,
        permission_mode: PermissionMode | None = None,
        data_dir: Path | None = None,
        resume: str | None = None,
        on_token: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        allow_destructive: bool = False,
    ) -> None:
        self.project_root = project_root.resolve()
        self.config = config
        self.model_ref = model_ref or config.defaults.model
        self._provider_override = provider
        self.console = console or Console()
        self.console_input = console_input or ConsoleInput()
        self.permission_mode = PermissionMode(permission_mode or config.defaults.permission_mode)
        self.data_dir = data_dir or trendlab_home()
        self.resume_target = resume
        self.on_token = on_token
        self.on_thinking = on_thinking
        self.events = EventBus()
        self.store: SessionStore | None = None
        self.session_id: str = ""
        self.resumed = False
        self.engine = PermissionEngine(
            mode=self.permission_mode,
            allow_destructive=allow_destructive or config.defaults.allow_destructive,
        )
        self.approvals: ApprovalManager | None = None
        self.local_channel: LocalTerminalChannel | None = None
        self.web_channel: WebApprovalChannel | None = None
        self.gateway: ModelGateway | None = None
        self.costs: CostTracker | None = None
        self.context: ContextManager | None = None
        self.plan = Plan()
        self.tools: ToolRuntime | None = None
        self.agent: AgentRuntime | None = None
        self.checkpoints: CheckpointManager | None = None
        self.repo_map: RepositoryMap | None = None
        self.hooks: Any = None
        self.mcp: Any = None
        self.skills: Any = None
        self.notifier: NotificationProvider | None = None
        self.subagents: SubAgentRunner | None = None
        self._token: str | None = None
        self._run_checkpoint: dict[str, Any] | None = None
        self._reminder_task: asyncio.Task | None = None
        self.pending_images: list[Path] = []  # attached via /paste or /image for the next prompt
        self.current_issue: dict[str, Any] | None = None
        self.main_project_root: Path = self.project_root

    # -- lifecycle -----------------------------------------------------------------------------
    async def start(self, *, interactive: bool = True, command_handler=None) -> None:
        self.store = SessionStore(self.data_dir / "sessions.db")
        machine = machine_name(self.config)
        self._open_session(machine)
        self.events.subscribe(JsonlEventSink(self.data_dir / "logs" / "events.jsonl"))
        self.events.subscribe(self._persist_event)

        self.notifier = create_notification_provider(self.config.notifications)
        self.approvals = ApprovalManager(
            config=self.config.remote_approval,
            events=self.events,
            store=self.store,
            session_id=self.session_id,
            machine=machine,
            project_name=self.project_root.name,
            notifier=self.notifier if "approval" in self.config.notifications.notify_on else None,
        )
        stale = self.approvals.recover()
        if stale:
            self.console.print(
                f"[yellow]Canceled {len(stale)} approval(s) left pending by a previous run "
                f"(they were never executed).[/yellow]"
            )
        if interactive:
            self.local_channel = LocalTerminalChannel(
                self.console, self.console_input, command_handler
            )
            await self.approvals.add_channel(self.local_channel)
        if self.config.remote_approval.enabled:
            await self.enable_remote(persist=False)

        self.gateway = ModelGateway(self.config, self.events, self.session_id)
        if self._provider_override is not None:
            self.gateway.register(self.model_ref, self._provider_override)
        self.costs = CostTracker(self.config)
        self.engine.project_rules = ProjectRules(self.project_root)

        rules = IgnoreRules.for_project(
            self.project_root, respect_gitignore=self.config.git.respect_gitignore
        )
        self.repo_map = RepositoryMap(
            self.project_root, rules, max_files=self.config.context.repo_map_max_files
        ).build()
        info = model_info(self.config, self.model_ref)
        self.context = ContextManager(
            self.config.context,
            self.events,
            self.session_id,
            system_prompt=build_system_prompt(self.project_root),
            context_window=info.context_window or self.config.context.default_context_window,
            summarizer=self._summarize,
        )
        self.context.repo_map_text = self.repo_map.render(
            self.config.context.repo_map_budget_tokens * 4
        )
        self.checkpoints = CheckpointManager(self.project_root, self.store, self.session_id)
        try:
            from trendlab.orchestration.gitflow import ensure_excluded

            ensure_excluded(self.project_root)
        except OSError:
            pass
        if self.resumed:
            self._restore_state()

        ctx = ToolContext(
            project_root=self.project_root,
            session_id=self.session_id,
            ignore_rules=rules,
            validation_commands=validation_commands(self.config, self.project_root),
        )
        registry = default_registry()
        registry.register(TaskTool(self.plan, self.events))
        registry.register(AskUserTool(self.approvals))
        self.tools = ToolRuntime(registry, self.engine, self.approvals, self.events, ctx)
        self.tools.on_before_mutation = self._checkpoint_before_mutation
        self.subagents = SubAgentRunner(
            config=self.config,
            gateway=self.gateway,
            events=self.events,
            session_id=self.session_id,
            session_model=self.model_ref,
            parent_registry=registry,
            parent_ctx=ctx,
            engine=self.engine,
            approvals=self.approvals,
            costs=self.costs,
        )
        registry.register(delegate_tool_factory(self.subagents))
        self.agent = AgentRuntime(
            gateway=self.gateway,
            model_ref=self.model_ref,
            tools=self.tools,
            context=self.context,
            events=self.events,
            session_id=self.session_id,
            costs=self.costs,
            limits=self.config.limits,
            plan=self.plan,
            escalation_model=self.config.routing.get("escalation"),
            on_token=self.on_token,
            on_thinking=self.on_thinking,
            on_message=self._persist_message,
            stream=self.on_token is not None,
        )
        await self._start_extensions()
        self.events.subscribe(self._notify_run_events)
        self.events.emit(
            EventType.SESSION_RESUMED if self.resumed else EventType.SESSION_STARTED,
            session_id=self.session_id,
            project=str(self.project_root),
            model=self.model_ref,
            mode=self.engine.mode.value,
            remote_enabled=self.approvals.remote_enabled,
            files_mapped=len(self.repo_map.files),
        )
        if self.hooks is not None:
            await self.hooks.run("session_start")
        if self.notifier is not None and self.config.notifications.reminder_at_fraction > 0:
            self._reminder_task = asyncio.create_task(self._reminder_loop())

    def _open_session(self, machine: str) -> None:
        assert self.store is not None
        target = self.resume_target
        row = None
        if target:
            row = (
                self.store.latest_session(str(self.project_root))
                if target == "latest"
                else self.store.get_session(target)
            )
            if row is None:
                self.console.print(
                    f"[yellow]No session to resume ({target}); starting a new one.[/yellow]"
                )
        if row is not None:
            self.session_id = row["id"]
            self.resumed = True
            if not self._provider_override and row.get("model"):
                self.model_ref = row["model"]
            self.store.touch_session(self.session_id)
        else:
            self.session_id = self.store.create_session(
                str(self.project_root), machine, self.model_ref
            )

    def _restore_state(self) -> None:
        assert self.store and self.context
        self.context.messages = self.store.messages(self.session_id)
        self.plan = Plan.from_json(self.store.get_state(self.session_id, "plan"))
        summary = self.store.get_state(self.session_id, "summary")
        if summary:
            from trendlab.context.compaction import CompactionRecord

            self.context.summary = CompactionRecord.model_validate(summary)
        self.events.emit(
            EventType.SESSION_RESTORED,
            session_id=self.session_id,
            messages=len(self.context.messages),
            tasks=len(self.plan.tasks),
        )

    async def _start_extensions(self) -> None:
        """Hooks, skills and MCP servers are optional; failures are reported, never fatal."""
        try:
            from trendlab.extensions.hooks import HookRunner

            self.hooks = HookRunner(
                self.config.hooks, self.project_root, self.events, self.session_id
            )
            self.tools.hooks = self.hooks  # type: ignore[union-attr]
            self.agent.hooks = self.hooks  # type: ignore[union-attr]
        except ImportError:
            pass
        try:
            from trendlab.extensions.skills import SkillLibrary

            self.skills = SkillLibrary(self.project_root, self.data_dir)
        except ImportError:
            pass
        servers = self.config.mcp_servers()
        if servers:
            try:
                from trendlab.extensions.mcp import McpManager

                self.mcp = McpManager(servers, self.events, self.session_id)
                await self.mcp.start(self.tools.registry)  # type: ignore[union-attr]
            except ImportError:
                pass

    async def _reminder_loop(self, interval: float = 30.0) -> None:
        while True:
            await asyncio.sleep(interval)
            if self.approvals is not None:
                try:
                    await self.approvals.send_reminders(
                        self.config.notifications.reminder_at_fraction
                    )
                except Exception:  # noqa: BLE001 — reminders are best effort
                    pass

    async def stop(self) -> None:
        if self._reminder_task is not None:
            self._reminder_task.cancel()
            self._reminder_task = None
        if self.hooks is not None:
            await self.hooks.run("session_end")
        if self.mcp is not None:
            await self.mcp.stop()
        if self.approvals:
            self.approvals.cancel_all("shutdown")
            for ch in self.approvals.channels:
                await self.approvals.remove_channel(ch)
        if self.notifier:
            await self.notifier.close()
        if self.gateway:
            await self.gateway.close()
        if self.store:
            self.store.set_session_status(self.session_id, "closed")
            self.store.close()

    # -- running -----------------------------------------------------------------------------------
    async def run_prompt(self, prompt: str, *, images: list[Path] | None = None) -> RunResult:
        """Run a prompt; ``@file.png`` references in the text and ``images`` become vision parts."""
        assert self.agent is not None
        self._run_checkpoint = None
        images = [*self.pending_images, *(images or [])]
        self.pending_images = []
        content = build_user_content(prompt, self.project_root, images)
        if isinstance(content, list):
            self.events.emit(
                EventType.IMAGES_ATTACHED,
                session_id=self.session_id,
                images=[Path(p["path"]).name for p in content if p.get("type") == "image_path"],
            )
        result = await self.agent.run(content)
        if self._run_checkpoint is not None and self.checkpoints is not None:
            self.checkpoints.seal(self._run_checkpoint["id"])
        self._save_state()
        if self.hooks is not None:
            await self.hooks.run("task_complete", status=result.status)
        return result

    async def _checkpoint_before_mutation(self, files: list[str]) -> None:
        if self.checkpoints is None:
            return
        if self._run_checkpoint is None:
            from trendlab.tools.git import git_head

            head = await git_head(self.project_root)
            self._run_checkpoint = self.checkpoints.create(files, label="auto", git_head=head)
            self.events.emit(
                EventType.SESSION_CHECKPOINTED,
                session_id=self.session_id,
                checkpoint=self._run_checkpoint["id"],
                files=files,
            )
        else:
            self.checkpoints.extend(self._run_checkpoint["id"], files)

    async def _summarize(self, messages: list[dict[str, Any]]) -> str:
        assert self.gateway and self.costs
        ref = resolve_role(self.config, "summarizer", self.model_ref)
        response, used = await self.gateway.complete(ref, messages, None)
        self.costs.record(
            used,
            response.usage,
            0,
            role="summarizer",
            local=self.gateway.provider(used).capabilities().local,
        )
        return response.text

    def _save_state(self) -> None:
        if self.store is None:
            return
        self.store.set_state(self.session_id, "plan", self.plan.to_json())
        if self.context and self.context.summary:
            self.store.set_state(
                self.session_id, "summary", self.context.summary.model_dump(mode="json")
            )
        self.store.touch_session(self.session_id)

    def _persist_message(self, message: dict[str, Any]) -> None:
        if self.store is not None:
            self.store.append_message(self.session_id, message)

    def _persist_event(self, event: Event) -> None:
        if self.store is None:
            return
        rec = event.to_record()
        self.store.append_event(event.session_id, rec.pop("event"), rec, rec.pop("ts"))
        if event.type == EventType.MODEL_CALL_COMPLETED:
            d = event.data
            self.store.record_model_call(
                self.session_id,
                d.get("model", "?"),
                d.get("role", "main"),
                d.get("input_tokens", 0),
                d.get("output_tokens", 0),
                d.get("cached_input_tokens", 0),
                d.get("latency_ms", 0),
                d.get("cost_usd", 0.0),
            )
        elif event.type == EventType.PLAN_UPDATED:
            self.store.set_state(self.session_id, "plan", event.data.get("plan"))

    def _notify_run_events(self, event: Event) -> None:
        """Phone notifications for completion/failure (separate from approvals)."""
        if self.notifier is None or not self.config.notifications.enabled:
            return
        kinds = {
            EventType.RUN_COMPLETED: "completion",
            EventType.RUN_FAILED: "failure",
            EventType.RUN_CANCELED: "failure",
            EventType.LOOP_DETECTED: "failure",
        }
        kind = kinds.get(event.type)
        if kind is None or kind not in self.config.notifications.notify_on:
            return
        if event.data.get("role", "main") != "main":
            return
        d = event.data
        title = {
            EventType.RUN_COMPLETED: f"{PRODUCT_NAME} — Task completed",
            EventType.RUN_FAILED: f"{PRODUCT_NAME} — Task stopped",
            EventType.RUN_CANCELED: f"{PRODUCT_NAME} — Task canceled",
            EventType.LOOP_DETECTED: f"{PRODUCT_NAME} — No progress",
        }[event.type]
        body_lines = [f"Machine: {machine_name(self.config)}", f"Project: {self.project_root.name}"]
        if d.get("stop_reason"):
            body_lines.append(f"Reason: {d['stop_reason']}")
        if d.get("reason"):
            body_lines.append(f"Loop: {d['reason']}")
        if d.get("changed_files"):
            body_lines.append(f"Changed: {', '.join(d['changed_files'][:5])}")
        if "validated" in d:
            body_lines.append(f"Validated: {'yes' if d['validated'] else 'no'}")
        if "cost_usd" in d:
            body_lines.append(f"Cost: ${d['cost_usd']:.3f}")
        notification = Notification(
            title=title, body="\n".join(body_lines), url=None, approval_id="", risk="low"
        )
        import asyncio

        async def _send() -> None:
            try:
                await self.notifier.send(notification)  # type: ignore[union-attr]
                self.events.emit(EventType.NOTIFICATION_SENT, session_id=self.session_id, kind=kind)
            except Exception as exc:  # noqa: BLE001
                self.events.emit(
                    EventType.NOTIFICATION_FAILED,
                    session_id=self.session_id,
                    kind=kind,
                    error=str(exc)[:200],
                )

        try:
            asyncio.get_running_loop().create_task(_send())
        except RuntimeError:
            pass

    # -- sessions -------------------------------------------------------------------------------
    def switch_session(self, target: str) -> str:
        """Resume another session in place: swap messages, plan, summary and ids. Returns the id."""
        assert self.store and self.context and self.agent and self.tools and self.approvals
        row = (
            self.store.latest_session(str(self.project_root))
            if target == "latest"
            else self.store.get_session(target)
        )
        if row is None:
            raise KeyError(target)
        if row["id"] == self.session_id:
            return self.session_id
        self._save_state()
        self.store.set_session_status(self.session_id, "closed")
        self.session_id = row["id"]
        self.resumed = True
        self.store.set_session_status(self.session_id, "active")
        self.store.touch_session(self.session_id)
        for obj in (self.approvals, self.agent, self.context, self.tools.ctx):
            obj.session_id = self.session_id
        if self.subagents is not None:
            self.subagents.session_id = self.session_id
        self.context.messages = self.store.messages(self.session_id)
        self.plan.tasks = Plan.from_json(self.store.get_state(self.session_id, "plan")).tasks
        summary = self.store.get_state(self.session_id, "summary")
        if summary:
            from trendlab.context.compaction import CompactionRecord

            self.context.summary = CompactionRecord.model_validate(summary)
        else:
            self.context.summary = None
        self.tools.changed_files.clear()
        self.tools.validation_runs.clear()
        if row.get("model") and not self._provider_override:
            try:
                self.switch_model(row["model"])
            except Exception:  # noqa: BLE001 — keep the current model if the old one is gone
                pass
        self.events.emit(
            EventType.SESSION_RESUMED,
            session_id=self.session_id,
            project=str(self.project_root),
            model=self.model_ref,
            messages=len(self.context.messages),
            in_place=True,
        )
        return self.session_id

    def export_transcript(self, path: Path | None = None) -> Path:
        """Write the session as Markdown (spec addendum: transcript export)."""
        assert self.store and self.context
        path = path or (
            self.project_root / ".trendlab" / "exports" / f"session-{self.session_id}.md"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# TrendLab session {self.session_id}",
            "",
            f"- project: `{self.project_root}`",
            f"- model: `{self.model_ref}`",
            f"- mode: `{self.engine.mode.value}`",
        ]
        if self.costs:
            lines.append(
                f"- cost: ${self.costs.total_usd:.4f} over {self.costs.model_calls} model calls"
            )
        lines.append("")
        if self.plan.tasks:
            lines += ["## Plan", "", "```", self.plan.render(), "```", ""]
        if self.context.summary:
            lines += ["## Compacted summary", "", self.context.summary.summary, ""]
        lines += ["## Transcript", ""]
        for m in self.store.messages(self.session_id):
            role = m.get("role")
            if role == "user":
                lines += [f"**You:** {text_of(m.get('content'))}", ""]
            elif role == "assistant":
                if m.get("content"):
                    lines += [f"**TrendLab:** {m['content']}", ""]
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    lines.append(f"- tool `{fn.get('name')}` {fn.get('arguments', '')[:300]}")
                if m.get("tool_calls"):
                    lines.append("")
            elif role == "tool":
                content = str(m.get("content") or "")
                lines += [
                    f"<details><summary>result of {m.get('name')}</summary>",
                    "",
                    "```",
                    content[:4000],
                    "```",
                    "</details>",
                    "",
                ]
        if self.tools and self.tools.changed_files:
            lines += (
                ["## Files changed", ""] + [f"- `{f}`" for f in self.tools.changed_files] + [""]
            )
        from trendlab.security.redaction import redact_text

        path.write_text(redact_text("\n".join(lines)), encoding="utf-8")
        return path

    # -- worktrees ------------------------------------------------------------------------------
    def switch_project_root(self, new_root: Path) -> None:
        """Point tools, checkpoints, repo map and permissions at another checkout (a worktree)."""
        assert self.tools and self.context and self.store
        new_root = new_root.resolve()
        self.project_root = new_root
        rules = IgnoreRules.for_project(
            new_root, respect_gitignore=self.config.git.respect_gitignore
        )
        self.tools.ctx.project_root = new_root
        self.tools.ctx.ignore_rules = rules
        self.tools.ctx.validation_commands = validation_commands(self.config, new_root)
        self.engine.project_rules = ProjectRules(new_root)
        self.repo_map = RepositoryMap(
            new_root, rules, max_files=self.config.context.repo_map_max_files
        ).build()
        self.context.repo_map_text = self.repo_map.render(
            self.config.context.repo_map_budget_tokens * 4
        )
        self.context.system_prompt = build_system_prompt(new_root)
        self.checkpoints = CheckpointManager(new_root, self.store, self.session_id)
        if self.subagents is not None:
            self.subagents.parent_ctx = self.tools.ctx
        self.events.emit(
            EventType.PROJECT_ROOT_CHANGED, session_id=self.session_id, root=str(new_root)
        )

    # -- model switching ---------------------------------------------------------------------------
    def switch_model(self, model_ref: str) -> None:
        assert self.gateway and self.agent and self.context
        self.gateway.provider(model_ref)  # validates configuration eagerly
        self.model_ref = model_ref
        self.agent.set_model(model_ref)
        if self.subagents is not None:
            self.subagents.session_model = model_ref
        info = model_info(self.config, model_ref)
        self.context.context_window = (
            info.context_window or self.config.context.default_context_window
        )
        if self.store:
            self.store._conn.execute(
                "UPDATE sessions SET model=? WHERE id=?", (model_ref, self.session_id)
            )  # noqa: SLF001
            self.store._conn.commit()  # noqa: SLF001

    def privacy_label(self, model_ref: str | None = None) -> str:
        assert self.gateway
        try:
            return self.gateway.provider(model_ref or self.model_ref).capabilities().privacy_label
        except Exception:  # noqa: BLE001
            return "UNKNOWN"

    # -- remote approval control -------------------------------------------------------------------
    @property
    def token(self) -> str:
        if self._token is None:
            self._token = load_or_create_token()
        return self._token

    async def enable_remote(self, *, persist: bool = True) -> str:
        assert self.approvals is not None
        if self.web_channel is None:
            channel = WebApprovalChannel(self.config.remote_approval, self.token, self.events)
            await self.approvals.add_channel(channel)  # raises ChannelError on bind/TLS problems
            self.web_channel = channel
        self.config.remote_approval.enabled = True
        self.approvals.approval_url = self.web_channel.url
        if persist:
            from trendlab.config.loader import update_global_config

            update_global_config("remote_approval", {"enabled": True})
        return self.web_channel.url

    async def disable_remote(self, *, persist: bool = True) -> None:
        assert self.approvals is not None
        if self.web_channel is not None:
            await self.approvals.remove_channel(self.web_channel)
            self.web_channel = None
        self.config.remote_approval.enabled = False
        self.approvals.approval_url = None
        if persist:
            from trendlab.config.loader import update_global_config

            update_global_config("remote_approval", {"enabled": False})

    def pairing_url(self) -> str | None:
        if self.web_channel is None:
            return None
        return pairing_url(self.web_channel.url, self.token)

    def remote_status(self) -> dict[str, Any]:
        cfg = self.config.remote_approval
        notif = self.config.notifications
        return {
            "enabled": cfg.enabled and self.web_channel is not None,
            "configured": cfg.enabled,
            "url": self.web_channel.url if self.web_channel else None,
            "bind": f"{cfg.host}:{cfg.port}",
            "tls": bool(self.web_channel and self.web_channel.tls),
            "timeout_minutes": cfg.request_timeout_minutes,
            "allow_high_risk": cfg.allow_high_risk,
            "allow_session_scope": cfg.allow_session_scope,
            "notifications": notif.provider if notif.enabled else "disabled",
            "pending": len(self.approvals.pending()) if self.approvals else 0,
        }
