"""Composition root: wires config → store → events → permissions → approvals → tools → agent.

UI layers (REPL today, Textual later) talk to ``TrendLabApp``; they contain no
agent or permission logic.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

from rich.console import Console

from trendlab.agent.prompt import build_system_prompt
from trendlab.agent.runtime import AgentRuntime
from trendlab.approvals.channels.local import LocalTerminalChannel
from trendlab.approvals.channels.web import WebApprovalChannel
from trendlab.approvals.manager import ApprovalManager
from trendlab.approvals.notifications.registry import create_notification_provider
from trendlab.approvals.tokens import load_or_create_token, pairing_url
from trendlab.config.loader import trendlab_home
from trendlab.config.schema import AppConfig, PermissionMode
from trendlab.permissions.engine import PermissionEngine
from trendlab.providers.base import ModelProvider
from trendlab.providers.registry import create_provider
from trendlab.sessions.store import SessionStore
from trendlab.telemetry.events import Event, EventBus, EventType, JsonlEventSink
from trendlab.tools.base import ToolContext
from trendlab.tools.registry import default_registry
from trendlab.tools.runtime import ToolRuntime
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
    ) -> None:
        self.project_root = project_root.resolve()
        self.config = config
        self.model_ref = model_ref or config.defaults.model
        self._provider_override = provider
        self.console = console or Console()
        self.console_input = console_input or ConsoleInput()
        self.permission_mode = permission_mode or config.defaults.permission_mode
        self.data_dir = data_dir or trendlab_home()
        self.events = EventBus()
        self.store: SessionStore | None = None
        self.session_id: str = ""
        self.engine = PermissionEngine(mode=self.permission_mode)
        self.approvals: ApprovalManager | None = None
        self.local_channel: LocalTerminalChannel | None = None
        self.web_channel: WebApprovalChannel | None = None
        self.agent: AgentRuntime | None = None
        self.provider: ModelProvider | None = None
        self._token: str | None = None

    # -- lifecycle ------------------------------------------------------------------
    async def start(self, *, interactive: bool = True, command_handler=None) -> None:
        self.store = SessionStore(self.data_dir / "sessions.db")
        self.session_id = self.store.create_session(
            str(self.project_root), machine_name(self.config), self.model_ref
        )
        self.events.subscribe(JsonlEventSink(self.data_dir / "logs" / "events.jsonl"))
        self.events.subscribe(self._persist_event)

        self.approvals = ApprovalManager(
            config=self.config.remote_approval,
            events=self.events,
            store=self.store,
            session_id=self.session_id,
            machine=machine_name(self.config),
            project_name=self.project_root.name,
            notifier=create_notification_provider(self.config.notifications),
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

        self.provider = self._provider_override or create_provider(self.config, self.model_ref)
        ctx = ToolContext(project_root=self.project_root, session_id=self.session_id)
        tool_runtime = ToolRuntime(
            default_registry(), self.engine, self.approvals, self.events, ctx
        )
        self.agent = AgentRuntime(
            provider=self.provider,
            tools=tool_runtime,
            events=self.events,
            session_id=self.session_id,
            system_prompt=build_system_prompt(self.project_root),
            max_iterations=self.config.limits.max_iterations,
        )
        self.events.emit(
            EventType.SESSION_STARTED,
            session_id=self.session_id,
            project=str(self.project_root),
            model=self.model_ref,
            mode=self.engine.mode.value,
            remote_enabled=self.approvals.remote_enabled,
        )

    async def stop(self) -> None:
        if self.approvals:
            self.approvals.cancel_all("shutdown")
            for ch in self.approvals.channels:
                await self.approvals.remove_channel(ch)
            if self.approvals.notifier:
                await self.approvals.notifier.close()
        if self.provider:
            await self.provider.close()
        if self.store:
            self.store.close()

    def _persist_event(self, event: Event) -> None:
        if self.store is not None:
            rec = event.to_record()
            self.store.append_event(event.session_id, rec.pop("event"), rec, rec.pop("ts"))

    # -- remote approval control --------------------------------------------------
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
