"""Composition root: config → store → events → permissions → approvals → gateway → context →
tools → agent. UI layers (REPL, Textual TUI) talk to ``TrendLabApp`` and contain no agent logic."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from rich.console import Console

from trendlab import PRODUCT_NAME
from trendlab.agent.plan_gate import PlanGate
from trendlab.agent.prompt import build_system_prompt
from trendlab.agent.runtime import AgentRuntime, RunResult
from trendlab.agent.state import AgentState
from trendlab.agent.tasks import Plan
from trendlab.approvals.channels.base import ChannelError
from trendlab.approvals.channels.local import LocalTerminalChannel
from trendlab.approvals.channels.telegram import TelegramChannel
from trendlab.approvals.channels.web import WebApprovalChannel
from trendlab.approvals.manager import ApprovalManager
from trendlab.approvals.notifications.base import Notification, NotificationProvider
from trendlab.approvals.notifications.registry import create_notification_provider
from trendlab.approvals.tokens import load_or_create_token, pairing_url
from trendlab.config.loader import trendlab_home
from trendlab.config.schema import AppConfig, PermissionMode
from trendlab.context.ignore import IgnoreRules
from trendlab.context.manager import ContextManager
from trendlab.context.project_memory import (
    LEARN_PROMPT,
    ProjectMemory,
    deterministic_facts,
    noteworthy,
    parse_facts,
)
from trendlab.context.repository_map import RepositoryMap
from trendlab.context.validation import validation_commands
from trendlab.orchestration.subagents import SubAgentRunner, delegate_tool_factory
from trendlab.permissions.engine import PermissionEngine
from trendlab.permissions.rules import ProjectRules
from trendlab.providers.base import ModelProvider
from trendlab.providers.catalog import cheapest_vision_model, supports_vision
from trendlab.providers.gateway import ModelGateway
from trendlab.providers.registry import model_info, resolve_role
from trendlab.remote.telegram_bridge import TelegramBridge, TelegramError
from trendlab.security.sandbox import Sandbox
from trendlab.sessions.checkpoints import CheckpointManager
from trendlab.sessions.store import SessionStore
from trendlab.telemetry.costs import CostTracker
from trendlab.telemetry.events import (
    TRANSIENT_EVENTS,
    Event,
    EventBus,
    EventType,
    JsonlEventSink,
)
from trendlab.tools.ask_user import AskUserTool
from trendlab.tools.background import BackgroundProcessManager
from trendlab.tools.base import ToolContext
from trendlab.tools.diagnostics import Diagnostics
from trendlab.tools.registry import default_registry
from trendlab.tools.runtime import ToolRuntime
from trendlab.tools.task_tool import TaskTool
from trendlab.ui.attachments import build_user_content, text_of
from trendlab.ui.console import ConsoleInput
from trendlab.ui.file_refs import expand_file_refs
from trendlab.ui.winpaths import translate_windows_paths


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
        self.telegram_channel: TelegramChannel | None = None
        self.plan_gate: PlanGate | None = None
        self.memory: ProjectMemory | None = None
        self._run_steering: list[str] = []
        self._run_failures = 0
        self._learn_task: asyncio.Task | None = None
        self.telegram_bridge: TelegramBridge | None = None
        # A UI may take over how remote prompts are started (TUI: its transcript + run task).
        self.on_remote_prompt: Callable[[str], Awaitable[None]] | None = None
        self._remote_task: asyncio.Task | None = None
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
        self.sandbox: Sandbox | None = None
        self.background: BackgroundProcessManager | None = None
        self.custom_commands: Any = None
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
            # With the Telegram button channel on, approvals already arrive as Telegram messages.
            notifier=(
                self.notifier
                if "approval" in self.config.notifications.notify_on
                and not self.config.remote_approval.telegram
                else None
            ),
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
        if self.config.sessions.record_cassettes:
            from trendlab.benchmarks.cassette import Recorder

            self._recorder = Recorder(lambda: self.session_id)
            self.gateway.recorder = self._recorder
        if self._provider_override is not None:
            self.gateway.register(self.model_ref, self._provider_override)
        self.costs = CostTracker(self.config)
        self.costs.on_record = self._persist_cost
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
            system_prompt=self._system_prompt_for(self.project_root),
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

        self.sandbox = Sandbox(self.config.sandbox, self.project_root)
        ctx = ToolContext(
            project_root=self.project_root,
            session_id=self.session_id,
            ignore_rules=rules,
            validation_commands=validation_commands(self.config, self.project_root),
            sandbox=self.sandbox,
            background=BackgroundProcessManager(self.project_root),
        )
        self.background = ctx.background
        registry = default_registry()
        registry.register(TaskTool(self.plan, self.events))
        registry.register(AskUserTool(self.approvals))
        self.tools = ToolRuntime(registry, self.engine, self.approvals, self.events, ctx)
        self.tools.allow_tools = set(self.config.tools.allow)
        self.tools.deny_tools = set(self.config.tools.deny)
        self.tools.tool_budgets = dict(self.config.context.tool_budgets)
        self.tools.screener_threshold_tokens = self.config.context.screener_threshold_tokens
        self.tools.screener = self._screen_output
        from trendlab.tools.views.tiering import Baselines

        self.tools.baselines = Baselines(self.project_root)
        self.tools.on_before_mutation = self._before_mutation
        if getattr(self, "_recorder", None) is not None:
            self.tools.recorder = self._recorder.tool
        self.plan_gate = PlanGate(
            self.config.plan_gate, self.approvals, self.plan, self.events, self.project_root
        )
        self.tools.diagnostics = Diagnostics(self.config.diagnostics, self.project_root)
        self.events.emit(
            EventType.SANDBOX_STATUS,
            session_id=self.session_id,
            status=self.sandbox.describe(),
            active=self.sandbox.active,
        )
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
        vcfg = self.config.verification
        self.agent.verifier = self._verify_change
        # The verifier needs a model of its own ([routing] verifier, else escalation); with
        # neither configured the review is off rather than a same-context self-review.
        has_verifier_model = bool(
            self.config.routing.get("verifier") or self.config.routing.get("escalation")
        )
        self.agent.verification_mode = vcfg.verifier if has_verifier_model else "off"
        self.agent.verify_min_diff_lines = vcfg.min_diff_lines
        self.agent.verify_min_files = vcfg.min_files
        self.agent.verify_min_confidence = vcfg.min_confidence
        self.agent.scope_config = self.config.governance
        self.agent.pre_run_sources = self._pre_run_sources
        if self.plan_gate is not None:
            self.agent.plan_gate_reset = self.plan_gate.reset
        if self.config.routing.get("router"):
            self.agent.router = self._route_request
        if self.config.context.retrieval:
            self.agent.retriever = self._retrieve
        self.agent.test_strength = self._test_strength
        if self.config.planner.enabled:
            self.agent.planner = self._plan_steps
            self.agent.planner_max_calls = self.config.planner.max_calls
            self.agent.planner_min_chars = self.config.planner.min_prompt_chars
        self.agent.design_checkpoint = self.config.planner.design_checkpoint
        self.agent.step_iterations = self.config.limits.step_iterations
        self.agent.best_of = self._best_of_for(self.model_ref)
        self.agent.candidates = self._run_candidates
        self.agent.verification_max_rounds = vcfg.max_rounds
        self.agent.regression_gate = vcfg.regression_gate
        await self._start_extensions()
        self.events.subscribe(self._notify_run_events)
        self.events.subscribe(self._track_run_signals)
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
        if self.config.telegram_bridge.enabled and os.environ.get(
            "TRENDLAB_TELEGRAM", ""
        ).lower() not in {
            "off",
            "0",
            "false",
        }:
            # TRENDLAB_TELEGRAM=off keeps a second process (tests, scripts, a parallel session)
            # from fighting the interactive session for the bot's single update stream.
            try:
                await self.enable_telegram_bridge()
            except TelegramError as exc:
                self.console.print(f"[yellow]Telegram remote control unavailable: {exc}[/yellow]")

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
        from trendlab.config.schema import HookConfig, McpServerConfig
        from trendlab.extensions.plugins import PluginManager

        self.plugins = PluginManager(self.data_dir)
        try:
            plugin_hooks = [HookConfig(**h) for h in self.plugins.hooks()]
            plugin_mcp = {k: McpServerConfig(**v) for k, v in self.plugins.mcp_servers().items()}
            command_dirs = self.plugins.command_dirs()
            skill_dirs = self.plugins.skill_dirs()
        except Exception as exc:  # noqa: BLE001 — a broken plugin never stops the session
            self.console.print(f"[warning]plugins not loaded: {exc}[/warning]")
            plugin_hooks, plugin_mcp, command_dirs, skill_dirs = [], {}, [], []
        try:
            from trendlab.extensions.hooks import HookRunner

            self.hooks = HookRunner(
                [*self.config.hooks, *plugin_hooks], self.project_root, self.events, self.session_id
            )
            self.tools.hooks = self.hooks  # type: ignore[union-attr]
            self.agent.hooks = self.hooks  # type: ignore[union-attr]
        except ImportError:
            pass
        try:
            from trendlab.extensions.skills import SkillLibrary

            self.skills = SkillLibrary(self.project_root, self.data_dir, skill_dirs)
            if self.agent is not None:
                self.agent.skills = self.skills
        except ImportError:
            pass
        try:
            from trendlab.extensions.commands import CustomCommandLibrary

            self.custom_commands = CustomCommandLibrary(
                self.project_root, self.data_dir, command_dirs
            )
        except ImportError:
            pass
        servers = {**plugin_mcp, **self.config.mcp_servers()}
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
        if self._learn_task is not None and not self._learn_task.done():
            self._learn_task.cancel()
        if self.telegram_bridge is not None:
            await self.telegram_bridge.stop()
            self.telegram_bridge = None
        if self.background is not None:
            await self.background.stop_all()
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
        self._checkpoint_step = None
        self._step_checkpoint = None
        if self.plan_gate is not None:
            self.plan_gate.reset()
        images = [*self.pending_images, *(images or [])]
        self.pending_images = []
        prompt, translated = translate_windows_paths(prompt)
        if translated:
            self.events.emit(
                EventType.PATHS_TRANSLATED, session_id=self.session_id, count=translated
            )
        prompt, attached = expand_file_refs(prompt, self.project_root)
        if attached:
            self.events.emit(EventType.FILES_ATTACHED, session_id=self.session_id, files=attached)
        content = build_user_content(prompt, self.project_root, images)
        restore_model: str | None = None
        if isinstance(content, list):
            self.events.emit(
                EventType.IMAGES_ATTACHED,
                session_id=self.session_id,
                images=[Path(p["path"]).name for p in content if p.get("type") == "image_path"],
            )
            if not supports_vision(self.config, self.model_ref):
                vision = cheapest_vision_model(self.config, self.model_ref)
                if vision and vision != self.model_ref:
                    restore_model = self.model_ref
                    self.switch_model(vision)
                    self.events.emit(
                        EventType.VISION_AUTOSWITCH,
                        session_id=self.session_id,
                        from_model=restore_model,
                        to_model=vision,
                    )
        self._run_steering = []
        self._run_failures = 0
        worktree = self._enter_worktree()
        locked_root = None
        if self.config.sessions.run_lock:
            from trendlab.sessions.runlock import acquire

            locked_root = self.tools.ctx.project_root if self.tools else self.project_root
            acquire(locked_root, self.session_id)  # raises RunLocked when another run holds it
        try:
            result = await self.agent.run(content)
        finally:
            if locked_root is not None:
                from trendlab.sessions.runlock import release

                release(locked_root)
            if worktree is not None:
                await self._surface_worktree(*worktree)
            if restore_model is not None:
                self.switch_model(restore_model)
        # Memory extraction runs in the background so a slow summarizer never holds the session.
        self._learn_task = asyncio.create_task(self._learn_guarded(result))
        if self._run_checkpoint is not None and self.checkpoints is not None:
            self.checkpoints.seal(self._run_checkpoint["id"])
        self._save_state()
        self._commit_turn(prompt, result)
        self._file_to_inbox(prompt, result)
        if self.hooks is not None:
            await self.hooks.run("task_complete", status=result.status)
        return result

    def _commit_turn(self, prompt: str, result: RunResult) -> None:
        """U9 prompt-level commits: snapshot the turn on trendlab/turns/<session>."""
        if not self.config.governance.commit_per_turn or not result.changed_files:
            return
        from trendlab.sessions.turns import commit_turn

        self._turns = getattr(self, "_turns", 0) + 1
        try:
            info = commit_turn(
                self.project_root,
                self.session_id,
                prompt=prompt if isinstance(prompt, str) else str(prompt),
                result=result,
                turn=self._turns,
            )
        except Exception as exc:  # noqa: BLE001 — a turn commit must never fail the run
            info = {"error": str(exc)[:200]}
        if info:
            self.events.emit(EventType.TURN_COMMITTED, session_id=self.session_id, **info)

    def _file_to_inbox(self, prompt: str, result: RunResult) -> None:
        """Failed runs and verifier rejections become inbox cards (cheap-model spec §7.2)."""
        if not self.config.engine.file_failed_runs:
            return
        verdict = (result.verification or {}).get("verdict")
        if result.status != "FAILED" and verdict != "fail":
            return
        try:
            from trendlab.engine.daemon import inbox_path
            from trendlab.engine.inbox import Inbox

            inbox = Inbox(inbox_path())
            try:
                findings = (result.verification or {}).get("findings") or []
                root_cause = findings[0].get("issue") if findings else (result.stop_reason or "")
                inbox.record(
                    project=str(self.project_root),
                    title=("verifier rejected: " if verdict == "fail" else "run failed: ")
                    + prompt.strip().splitlines()[0][:120],
                    signature=f"{verdict or result.stop_reason}|{root_cause}",
                    source="verifier" if verdict == "fail" else "run",
                    root_cause=str(root_cause)[:400],
                    impacted_files=list(result.changed_files)[:12],
                    evidence=[f"session {self.session_id}"],
                    severity="high" if verdict == "fail" else "med",
                )
            finally:
                inbox.close()
        except Exception:  # noqa: BLE001 — the inbox never breaks a run
            pass

    # -- step-scoped execution (cheap-model spec §3.3, §4) -----------------------------------------
    async def _plan_steps(self, task_text: str, note: str = "") -> list[dict[str, Any]] | None:
        """One call to the planning role → verifiable steps (≤ planner.max_calls per run)."""
        from trendlab.agent.planner import build_messages, parse_steps

        assert self.gateway is not None and self.costs is not None and self.context is not None
        ref = resolve_role(self.config, "planning", self.model_ref)
        task = (
            task_text
            if not note
            else f"{task_text}\n\nRe-plan note: {note}\n\nCurrent plan:\n{self.plan.render()}"
        )
        ctx_text = (self.context.repo_map_text or "")[:4000]
        messages = build_messages(
            task, ctx_text, self.tools.ctx.validation_commands if self.tools else None
        )
        from trendlab.providers.structured_json import ask_json

        async def call(msgs):
            response, used = await self.gateway.complete(ref, msgs, None, role="planner")
            self.costs.record(
                used,
                response.usage,
                0,
                role="planner",
                local=self.gateway.provider(used).capabilities().local,
            )
            return response.text

        steps, _ = await ask_json(call, messages, parse_steps)
        # Pre-implementation review (U7): a deterministic lint; one repair call only when it
        # finds problems, and the repaired plan is kept only if it is no worse.
        from trendlab.agent.planner import lint_plan, repair_messages

        validation = self.tools.ctx.validation_commands if self.tools else None
        root = self.project_root

        def exists(rel: str) -> bool:
            return (root / rel).exists()

        before = lint_plan(steps or [], exists=exists, validation=validation) if steps else []
        after = before
        if steps and before:
            shown = json.dumps(
                {
                    "design": steps[0].get("_design"),
                    "steps": [
                        {k: v for k, v in st.items() if k not in ("_index", "_design")}
                        for st in steps
                    ],
                }
            )
            fixed, _ = await ask_json(call, repair_messages(messages, shown, before), parse_steps)
            if fixed:
                after = lint_plan(fixed, exists=exists, validation=validation)
                if len(after) <= len(before):
                    steps = fixed
                else:
                    after = before
        if self.agent is not None:
            self.agent.plan_review = {
                "issues": before,
                "remaining": after,
                "repaired": bool(before),
            }
        return steps

    def _retrieve(self, task_text: str):
        from trendlab.context.retrieval import hints_message, index_project
        from trendlab.context.vectors import VectorIndex, index_path, make_embedder, retrieve

        cfg = self.config.context
        rules = self.tools.ctx.ignore_rules if self.tools else None
        chunks = index_project(self.project_root, rules)
        mode, index = cfg.retrieval_mode, None
        if mode != "bm25" and cfg.embeddings:
            try:
                index = VectorIndex(
                    index_path(self.project_root), make_embedder(self.config, cfg.embeddings)
                )
            except Exception:  # noqa: BLE001 — no embedder: lexical retrieval still works
                mode = "bm25"
        try:
            hits = retrieve(chunks, task_text, mode=mode, index=index, k=cfg.retrieval_k)
        except Exception:  # noqa: BLE001 — embedding service down: fall back to BM25
            hits = retrieve(chunks, task_text, mode="bm25", k=cfg.retrieval_k)
        return hints_message(hits, cfg.retrieval_max_chars), hits

    async def _route_request(self, prompt: str):
        """Semantic routing: a cheap model classifies the request before the run."""
        from trendlab.agent.router import model_route

        assert self.gateway is not None and self.costs is not None
        ref = self.config.routing["router"]

        async def call(messages):
            response, used = await self.gateway.complete(ref, messages, None, role="router")
            self.costs.record(
                used,
                response.usage,
                0,
                role="router",
                local=self.gateway.provider(used).capabilities().local,
            )
            return response.text

        return await model_route(call, prompt)

    def _best_of_for(self, model_ref: str) -> int:
        cfg = self.config.attempts
        if isinstance(cfg.best_of, int):
            return cfg.best_of
        from trendlab.providers.base import TokenUsage

        price = CostTracker(self.config).price(model_ref, TokenUsage(input_tokens=1_000_000))
        local = bool(model_info(self.config, model_ref).local) or model_ref.startswith("ollama:")
        return 3 if (local or price < cfg.cheap_price_per_m) else 1

    async def _run_candidates(self, task, failure_tail: str, n: int) -> dict[str, Any]:
        from trendlab.orchestration.candidates import CandidateRunner

        assert self.tools is not None and self.costs is not None
        runner = CandidateRunner(
            config=self.config,
            events=self.events,
            session_id=self.session_id,
            model_ref=self.model_ref,
            registry=self.tools.registry,
            engine=self.engine,
            approvals=self.approvals,
            costs=self.costs,
            parent_ctx=self.tools.ctx,
        )
        runner.judge = self._pairwise_judge
        return await runner.run_best_of(task, failure_tail, n)

    async def _pairwise_judge(self, task, patch_a: str, patch_b: str) -> dict[str, Any]:
        from trendlab.agent.judge import pairwise

        assert self.gateway is not None and self.costs is not None
        ref = (
            self.config.routing.get("verifier")
            or self.config.routing.get("escalation")
            or self.model_ref
        )

        async def call(messages):
            response, used = await self.gateway.complete(ref, messages, None, role="judge")
            self.costs.record(
                used,
                response.usage,
                0,
                role="judge",
                local=self.gateway.provider(used).capabilities().local,
            )
            return response.text

        brief = f"{task.title}. Done when: {task.done_when or 'validation passes'}"
        return await pairwise(call, task=brief, a=patch_a, b=patch_b)

    # -- generated-test strength (uplift U5) ------------------------------------------------------
    def _pre_run_sources(self) -> dict[str, str | None] | None:
        """Files as they were before this run, from the run checkpoint (None = did not exist)."""
        if self._run_checkpoint is None or self.checkpoints is None:
            return None
        snap = self.checkpoints.dir / self._run_checkpoint["id"]
        out: dict[str, str | None] = {}
        # every mutated file is snapshotted before its first edit; no snapshot = it was created
        for rel in self.tools.changed_files if self.tools else {}:
            pre = snap / rel
            out[rel] = pre.read_text(errors="replace") if pre.is_file() else None
        return out

    async def _test_strength(self, changed: dict[str, list[str]]) -> str:
        """Does the new test fail without the fix? Source files are restored from this run's
        checkpoint snapshot, the test command runs, then the current files are put back."""
        import shutil
        import tempfile

        from trendlab.agent.scope import is_test_path

        cmd = (self.tools.ctx.validation_commands or {}).get("test") if self.tools else None
        if not cmd or self._run_checkpoint is None or self.checkpoints is None:
            return "unknown"
        snap = self.checkpoints.dir / self._run_checkpoint["id"]
        sources = [f for f in changed if not is_test_path(f)]
        if not sources:
            return "n/a"
        backup = Path(tempfile.mkdtemp(prefix="trendlab-strength-"))
        try:
            restored = 0
            for rel in sources:
                cur, pre = self.project_root / rel, snap / rel
                if cur.is_file():
                    (backup / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(cur, backup / rel)
                if pre.is_file():
                    shutil.copy2(pre, cur)
                    restored += 1
                elif cur.is_file():
                    cur.unlink()  # the fix created this file; without the fix it is absent
                    restored += 1
            if not restored:
                return "unknown"
            argv = (
                self.sandbox.wrap(cmd, cwd=self.project_root)
                if self.sandbox
                else ["/bin/sh", "-c", cmd]
            )
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self.project_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=300)
            except TimeoutError:
                proc.kill()
                return "unknown"
            return "weak" if proc.returncode == 0 else "strong"
        finally:
            for rel in sources:
                cur, saved = self.project_root / rel, backup / rel
                if saved.is_file():
                    cur.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(saved, cur)
                elif cur.exists():
                    cur.unlink()
            shutil.rmtree(backup, ignore_errors=True)

    # -- verify-then-surface (cheap-model spec §3.2, §5) ------------------------------------------
    async def _verify_change(self, task_text: str, ev, small: bool = False) -> Any:
        """Fresh-context verifier call: task + diff + latest validation + plan → verdict.
        Small validated changes are reviewed by the session (cheap) model; the rest by
        [routing] verifier / escalation."""
        from trendlab.agent.verifier import verify

        assert self.gateway is not None and self.costs is not None and self.tools is not None
        ref = (
            self.model_ref
            if small
            else (
                self.config.routing.get("verifier")
                or self.config.routing.get("escalation")
                or self.model_ref
            )
        )

        async def call(messages):
            response, used = await self.gateway.complete(ref, messages, None, role="verifier")
            self.costs.record(
                used,
                response.usage,
                0,
                role="verifier",
                local=self.gateway.provider(used).capabilities().local,
            )
            return response.text, used

        diff = self._diff_for_review(ev.changed_files)
        plan = self.plan.render() if hasattr(self.plan, "render") else ""
        verdict = await verify(
            call, task=task_text, diff=diff, validation=ev.last_validation, plan=plan
        )
        second = self.config.verification.second_opinion
        if verdict is not None and second and second != ref:
            # U2 judge agreement: a second verifier model on the same evidence
            async def call2(messages):
                response, used = await self.gateway.complete(
                    second, messages, None, role="verifier"
                )
                self.costs.record(
                    used,
                    response.usage,
                    0,
                    role="verifier",
                    local=self.gateway.provider(used).capabilities().local,
                )
                return response.text, used

            try:
                other = await verify(
                    call2, task=task_text, diff=diff, validation=ev.last_validation, plan=plan
                )
            except Exception:  # noqa: BLE001
                other = None
            if other is not None:
                self.events.emit(
                    EventType.VERIFY_SECOND_OPINION,
                    session_id=self.session_id,
                    first=verdict.verdict,
                    second=other.verdict,
                    agree=verdict.verdict == other.verdict,
                    models=[ref, second],
                )
                if other.verdict == "fail" and verdict.verdict != "fail":
                    verdict.findings = other.findings or verdict.findings
                    verdict.verdict = "fix" if verdict.verdict == "pass" else verdict.verdict
        return verdict

    def _diff_for_review(self, files: list[str]) -> str:
        """The per-edit diffs the tools recorded this run; whole file when a diff is missing."""
        assert self.tools is not None
        parts: list[str] = []
        for f in files:
            diffs = [d for d in self.tools.changed_files.get(f, []) if d]
            if diffs:
                parts.append("\n".join(diffs))
                continue
            path = self.project_root / f
            try:
                body = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
            except OSError:
                body = ""
            parts.append(
                f"+++ {f} (new or rewritten)\n{body[:8000]}" if body else f"--- {f} (deleted)"
            )
        return "\n\n".join(parts)

    def _enter_worktree(self) -> tuple[Path, Path, str] | None:
        """Worktree workspace: edit in .trendlab/worktrees/<run>, surface only a verified diff."""
        if self.config.verification.workspace != "worktree":
            return None
        if not (self.project_root / ".git").exists():
            self.events.emit(
                EventType.WORKTREE_RUN,
                session_id=self.session_id,
                outcome="skipped",
                reason="not a git repository",
            )
            return None
        from trendlab.orchestration.gitflow import WorktreeManager

        name = f"run-{secrets.token_hex(4)}"
        main_root = self.project_root
        try:
            wt = WorktreeManager(main_root).create_sync(name)
        except Exception as exc:  # noqa: BLE001 — fall back to editing in place
            self.events.emit(
                EventType.WORKTREE_RUN,
                session_id=self.session_id,
                outcome="skipped",
                reason=str(exc)[:200],
            )
            return None
        self.switch_project_root(wt)
        self.events.emit(
            EventType.WORKTREE_RUN, session_id=self.session_id, outcome="entered", name=name
        )
        return main_root, wt, name

    async def _surface_worktree(self, main_root: Path, wt: Path, name: str) -> None:
        """Apply the worktree's diff to the main tree when the run completed (and the verifier
        did not reject it); otherwise park it as a patch. The worktree is always removed."""
        from trendlab.tools.git import run_git

        assert self.agent is not None
        outcome, detail = "nothing", ""
        patch = ""
        try:
            from trendlab.orchestration.gitflow import worktree_patch

            patch, _files = await worktree_patch(
                wt, self.tools.ctx.ignore_rules if self.tools else None
            )
        except Exception as exc:  # noqa: BLE001
            detail = str(exc)[:200]
        completed = self.agent.state.state == AgentState.COMPLETED
        patch_file: Path | None = None
        if patch.strip():
            if completed:
                check, msg = await _git_apply(main_root, patch, "--check")
                if check == 0:
                    check, msg = await _git_apply(main_root, patch, "--index")
                outcome = "applied" if check == 0 else "parked"
                if check != 0:
                    detail = msg.strip()[:200]
            else:
                outcome = "parked"
            if outcome == "parked":
                patch_dir = main_root / ".trendlab" / "patches"
                patch_dir.mkdir(parents=True, exist_ok=True)
                patch_file = patch_dir / f"{name}.patch"
                patch_file.write_text(patch, encoding="utf-8")
        self.switch_project_root(main_root)
        try:
            await run_git(main_root, "worktree", "remove", "--force", str(wt))
            await run_git(main_root, "branch", "-D", f"trendlab/{name}")
        except Exception:  # noqa: BLE001
            pass
        self.events.emit(
            EventType.WORKTREE_RUN,
            session_id=self.session_id,
            outcome=outcome,
            name=name,
            patch=str(patch_file.relative_to(main_root)) if patch_file else None,
            reason=detail,
            completed=completed,
        )

    async def _before_mutation(self, files: list[str]) -> None:
        allow = self.config.governance.change_allow
        if allow:
            from trendlab.agent.scope import ChangeScopeRefused, outside_scope

            outside = outside_scope(files, allow, self.project_root)
            if outside:
                raise ChangeScopeRefused(outside, allow)
        if self.plan_gate is not None:
            await self.plan_gate.check(files)  # raises PlanRejected → mutation refused
        await self._checkpoint_before_mutation(files)

    # -- project memory --------------------------------------------------------------------------
    def _system_prompt_for(self, root: Path) -> str:
        base = build_system_prompt(root)
        if self.config.memory.enabled:
            if self.memory is None or self.memory.path.parent.parent != root:
                self.memory = ProjectMemory(root, max_entries=self.config.memory.max_entries)
            base += self.memory.render_for_prompt()
        if self.config.prompts.drivers:
            from trendlab.prompts.drivers import driver_text

            base += driver_text(self.config, self.model_ref, root, self.data_dir)
        from trendlab.agent.style import rules_text

        base += rules_text(self.config.governance)  # U9: stated before, checked after
        return base

    def refresh_system_prompt(self) -> None:
        """Rebuild the system prompt (memory changed, skills changed); applied at the next run."""
        if self.context is None:
            return
        prompt = self._system_prompt_for(self.project_root)
        if self.skills is not None and getattr(self.skills, "active", None):
            prompt = self.skills.apply(prompt)
        self.context.system_prompt = prompt

    def _track_run_signals(self, event: Event) -> None:
        if event.type == EventType.STEERED:
            self._run_steering.append(str(event.data.get("text", "")))
        elif event.type == EventType.TOOL_COMPLETED and not event.data.get("ok", True):
            self._run_failures += 1
        elif event.type == EventType.TOOL_SKIPPED:
            self._run_failures += 1

    async def _learn_guarded(self, result: RunResult) -> None:
        try:
            await asyncio.wait_for(self.learn_from_run(result), timeout=90)
        except (TimeoutError, asyncio.CancelledError):
            pass
        except Exception:  # noqa: BLE001 — never surface memory problems as run failures
            pass

    async def wait_for_learning(self) -> None:
        """Block until background memory extraction for the last run has finished (tests)."""
        if self._learn_task is not None and not self._learn_task.done():
            await self._learn_task

    async def learn_from_run(self, result: RunResult) -> list[str]:
        """Extract durable project facts from a finished run into .trendlab/memory.md."""
        if self.memory is None or not (self.config.memory.enabled and self.config.memory.learn):
            return []
        evidence = {
            "changed_files": list(result.changed_files),
            "validation_runs": list(result.validation_runs),
            "steering": list(self._run_steering),
            "failures": self._run_failures,
            "status": result.status,
            "stop_reason": result.stop_reason,
        }
        if not noteworthy(evidence) or result.model_calls < 2:
            return []
        facts: list[str] = []
        try:
            msgs = self.agent.messages[-24:] if self.agent else []
            lines = []
            for m in msgs:
                content = m.get("content")
                if isinstance(content, list):
                    content = text_of(content)
                text = " ".join(str(content or "").split())[:400]
                if text:
                    lines.append(f"{m.get('role')}: {text}")
            prompt = LEARN_PROMPT.format(
                existing="\n".join(f"- {f}" for f in self.memory.facts[-30:]) or "(empty)",
                evidence=json.dumps(evidence, default=str)[:2000],
                transcript="\n".join(lines)[-6000:],
            )
            answer = await self._summarize([{"role": "user", "content": prompt}])
            facts = parse_facts(answer)
        except Exception:  # noqa: BLE001 — learning is best effort
            facts = deterministic_facts(evidence)
        added = self.memory.add_many(facts)
        if added:
            self.refresh_system_prompt()
            self.events.emit(
                EventType.MEMORY_UPDATED,
                session_id=self.session_id,
                added=added,
                total=len(self.memory.facts),
                path=str(self.memory.path),
            )
        return added

    async def _checkpoint_before_mutation(self, files: list[str]) -> None:
        if self.checkpoints is None:
            return
        step = self.plan.active if self.plan else None
        if self._run_checkpoint is None:
            from trendlab.tools.git import git_head

            head = await git_head(self.project_root)
            self._run_checkpoint = self.checkpoints.create(files, label="auto", git_head=head)
            self._checkpoint_step = None
            self.events.emit(
                EventType.SESSION_CHECKPOINTED,
                session_id=self.session_id,
                checkpoint=self._run_checkpoint["id"],
                files=files,
            )
        else:
            # the run checkpoint keeps every file's pre-run content (test strength, arch check)
            self.checkpoints.extend(self._run_checkpoint["id"], files)
        if step is not None and step.id != getattr(self, "_checkpoint_step", None):
            # natural task checkpoint (U28): the files as they were when this step began
            self._checkpoint_step = step.id
            self._step_checkpoint = self.checkpoints.create(
                files, label=f"step {step.id}: {step.title[:60]}"
            )
        elif step is not None and getattr(self, "_step_checkpoint", None):
            self.checkpoints.extend(self._step_checkpoint["id"], files)

    async def _screen_output(self, text: str, meta: dict[str, Any]):
        """Screener (cheap-model spec §3.1): a cheap model turns an unparsed, oversized tool
        result into Tier 1 facts + Tier 2 detail. Returns None when unavailable."""
        if self.gateway is None or self.costs is None:
            return None
        from trendlab.tools.views.screener import screen

        ref = resolve_role(self.config, "screener", self.model_ref)

        async def call(messages):
            response, used = await self.gateway.complete(ref, messages, None, role="screener")
            self.costs.record(
                used,
                response.usage,
                0,
                role="screener",
                local=self.gateway.provider(used).capabilities().local,
            )
            return response.text

        return await screen(call, text, meta)

    async def _summarize(self, messages: list[dict[str, Any]]) -> str:
        assert self.gateway and self.costs
        ref = resolve_role(self.config, "summarizer", self.model_ref)
        response, used = await self.gateway.complete(ref, messages, None, role="summarizer")
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

    def _persist_cost(self, rec) -> None:
        """Every model call of every role lands in the store (fully loaded cost, U10)."""
        if self.store is not None:
            self.store.record_model_call(
                self.session_id,
                rec.model_ref,
                rec.role,
                rec.input_tokens,
                rec.output_tokens,
                rec.cached_input_tokens,
                rec.latency_ms,
                rec.cost_usd,
            )

    def _persist_message(self, message: dict[str, Any]) -> None:
        if self.store is not None:
            self.store.append_message(self.session_id, message)

    def _persist_event(self, event: Event) -> None:
        if self.store is None or event.type.value in TRANSIENT_EVENTS:
            return
        rec = event.to_record()
        self.store.append_event(event.session_id, rec.pop("event"), rec, rec.pop("ts"))
        if event.type == EventType.PLAN_UPDATED:
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
    def branch_session(self, label: str | None = None, *, keep_last: int | None = None) -> str:
        """Fork the current conversation into a child session and continue there.

        The parent keeps everything it has; the child starts with the same messages (optionally
        only the first ``len - keep_last``), plan and summary, and records its parent/branch point.
        """
        assert self.store and self.context
        self._save_state()
        total = len(self.store.messages(self.session_id))
        upto = None if keep_last is None else max(0, total - keep_last)
        new_id = self.store.fork_session(
            self.session_id, machine_name(self.config), self.model_ref, upto=upto, label=label
        )
        self.events.emit(
            EventType.SESSION_BRANCHED,
            session_id=self.session_id,
            child=new_id,
            branch_point=upto if upto is not None else total,
            label=label,
        )
        self.switch_session(new_id)
        return new_id

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
        self.sandbox = Sandbox(self.config.sandbox, new_root)
        self.tools.ctx.sandbox = self.sandbox
        self.background = self.tools.ctx.background = BackgroundProcessManager(new_root)
        if self.custom_commands is not None:
            self.custom_commands = type(self.custom_commands)(new_root, self.data_dir)
        self.tools.diagnostics = Diagnostics(self.config.diagnostics, new_root)
        self.repo_map = RepositoryMap(
            new_root, rules, max_files=self.config.context.repo_map_max_files
        ).build()
        self.context.repo_map_text = self.repo_map.render(
            self.config.context.repo_map_budget_tokens * 4
        )
        self.memory = None
        self.context.system_prompt = self._system_prompt_for(new_root)
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
        self.refresh_system_prompt()  # the model notes layer follows the model
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
        if self.config.remote_approval.telegram and self.telegram_channel is None:
            tg = TelegramChannel(
                self.config.notifications.telegram, self.events, project_name=self.project_root.name
            )
            if self.telegram_bridge is not None and self.telegram_bridge.poller is not None:
                tg.poller = self.telegram_bridge.poller  # share the bot's single update stream
            try:
                await self.approvals.add_channel(tg)
                self.telegram_channel = tg
            except ChannelError as exc:
                self.console.print(f"[yellow]Telegram approvals unavailable: {exc}[/yellow]")
        if persist:
            from trendlab.config.loader import update_global_config

            update_global_config("remote_approval", {"enabled": True})
        return self.web_channel.url

    async def disable_remote(self, *, persist: bool = True) -> None:
        assert self.approvals is not None
        if self.web_channel is not None:
            await self.approvals.remove_channel(self.web_channel)
            self.web_channel = None
        if self.telegram_channel is not None:
            await self.approvals.remove_channel(self.telegram_channel)
            self.telegram_channel = None
        self.config.remote_approval.enabled = False
        self.approvals.approval_url = None
        if persist:
            from trendlab.config.loader import update_global_config

            update_global_config("remote_approval", {"enabled": False})

    # -- Telegram remote control -----------------------------------------------------------------
    async def enable_telegram_bridge(self) -> TelegramBridge:
        if self.telegram_bridge is not None and self.telegram_bridge.running:
            return self.telegram_bridge
        bridge = TelegramBridge(
            self, self.config.telegram_bridge, self.config.notifications.telegram, self.events
        )
        await bridge.start()  # raises TelegramError when token/chat_id are missing
        self.telegram_bridge = bridge
        self.config.telegram_bridge.enabled = True
        return bridge

    async def disable_telegram_bridge(self) -> None:
        if self.telegram_bridge is not None:
            await self.telegram_bridge.stop()
            self.telegram_bridge = None
        self.config.telegram_bridge.enabled = False

    def run_in_progress(self) -> bool:
        if self._remote_task is not None and not self._remote_task.done():
            return True
        agent = self.agent
        return bool(agent and not agent.state.terminal and agent.state.state.value != "IDLE")

    def cancel_run(self) -> None:
        if self.agent is not None:
            self.agent.cancel()
        if self._remote_task is not None and not self._remote_task.done():
            self._remote_task.cancel()

    def remote_prompt(self, text: str) -> None:
        """Start a prompt that arrived from a remote surface; the UI shows it if one is attached."""
        if self.on_remote_prompt is not None:
            asyncio.get_running_loop().create_task(self.on_remote_prompt(text))
            return

        async def _run() -> None:
            self.console.print(f"[bold]📱 remote ❯[/bold] {text}")
            try:
                result = await self.run_prompt(text)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                self.console.print(f"[red]error: {exc}[/red]")
                return
            self.console.print(result.report)

        self._remote_task = asyncio.get_running_loop().create_task(_run())

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
            "telegram": self.telegram_channel.status() if self.telegram_channel else None,
            "telegram_bridge": self.telegram_bridge.status() if self.telegram_bridge else None,
            "plan_gate": self.plan_gate.status() if self.plan_gate else None,
            "pending": len(self.approvals.pending()) if self.approvals else 0,
        }


async def _git_apply(root: Path, patch: str, *flags: str) -> tuple[int, str]:
    """``git apply`` with the patch on stdin (never a temp file: sandboxes cannot see those)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "apply",
        *flags,
        "-",
        cwd=str(root),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(patch.encode("utf-8")), timeout=60)
    except TimeoutError:
        proc.kill()
        return 124, "git apply timed out"
    return proc.returncode or 0, out.decode("utf-8", "replace")
