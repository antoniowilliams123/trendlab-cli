"""Lifecycle hooks (spec §39): user-configured shell commands with explicit timeouts.

Events: session_start, before_model_call, after_model_call, before_tool, after_tool,
before_write, after_write, task_complete, session_end. ``before_*`` hooks marked
``blocking`` veto the operation when they exit non-zero.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from trendlab.config.schema import HookConfig
from trendlab.telemetry.events import EventBus, EventType

MUTATING = {"write_file", "patch_file", "apply_patch", "delete_file"}


class HookRunner:
    def __init__(
        self, hooks: list[HookConfig], project_root: Path, events: EventBus, session_id: str
    ) -> None:
        self.hooks = hooks
        self.root = project_root
        self.events = events
        self.session_id = session_id

    def _for(self, event: str) -> list[HookConfig]:
        return [h for h in self.hooks if h.event == event]

    async def run(self, event: str, **payload: Any) -> str | None:
        """Run all hooks for ``event``. Returns a veto message if a blocking hook failed."""
        veto = None
        for hook in self._for(event):
            code, out = await self._exec(hook, event, payload)
            self.events.emit(
                EventType.HOOK_RUN,
                session_id=self.session_id,
                event=event,
                command=hook.command,
                exit_code=code,
                output=out[-300:],
            )
            if code != 0 and hook.blocking and event.startswith("before_"):
                veto = veto or f"{hook.command} exited {code}: {out.strip()[-200:]}"
                self.events.emit(
                    EventType.HOOK_BLOCKED,
                    session_id=self.session_id,
                    event=event,
                    command=hook.command,
                    exit_code=code,
                )
        return veto

    async def _exec(self, hook: HookConfig, event: str, payload: dict[str, Any]) -> tuple[int, str]:
        env = dict(os.environ)
        env["TRENDLAB_EVENT"] = event
        env["TRENDLAB_PROJECT"] = str(self.root)
        env["TRENDLAB_SESSION"] = self.session_id
        for k, v in payload.items():
            env[f"TRENDLAB_{k.upper()}"] = v if isinstance(v, str) else json.dumps(v, default=str)
        try:
            proc = await asyncio.create_subprocess_shell(
                hook.command,
                cwd=str(self.root),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=hook.timeout_seconds)
            return proc.returncode or 0, out.decode("utf-8", "replace")
        except TimeoutError:
            proc.kill()
            return 124, f"hook timed out after {hook.timeout_seconds}s"
        except OSError as exc:
            return 127, str(exc)

    # -- convenience wrappers used by ToolRuntime --------------------------------------
    async def before_tool(self, tool: str, perm) -> str | None:
        veto = await self.run(
            "before_tool", tool=tool, command=perm.command or "", files=perm.affected_files
        )
        if veto is None and tool in MUTATING:
            veto = await self.run("before_write", tool=tool, files=perm.affected_files)
        return veto

    async def after_tool(self, tool: str, perm, result) -> None:
        await self.run("after_tool", tool=tool, ok=result.ok, files=perm.affected_files)
        if tool in MUTATING and result.ok:
            await self.run("after_write", tool=tool, files=perm.affected_files)
