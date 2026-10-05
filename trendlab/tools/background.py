"""Background processes (spec §90): start a dev server or watcher, keep working, read its logs.

Every process is tracked by a short id; stdout+stderr stream to ``.trendlab/bg/<id>.log`` inside
the project so the agent (and ``/bg logs``) can tail them. Processes die with the session.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from trendlab.permissions.classifier import classify_shell_command
from trendlab.permissions.engine import PermissionRequest
from trendlab.permissions.models import OperationCategory
from trendlab.security.redaction import redact_text
from trendlab.tools.base import Tool, ToolContext, ToolResult

MAX_PROCESSES = 8


@dataclass
class BackgroundProcess:
    id: str
    name: str
    command: str
    cwd: Path
    log_path: Path
    started_at: float
    proc: asyncio.subprocess.Process
    _log_fh: Any = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.proc.returncode is None

    def status(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "command": self.command,
            "pid": self.proc.pid,
            "running": self.running,
            "exit_code": self.proc.returncode,
            "uptime_s": round(time.monotonic() - self.started_at, 1),
            "log": str(self.log_path),
        }

    def tail(self, lines: int = 60) -> str:
        try:
            data = self.log_path.read_bytes()
        except OSError:
            return ""
        text = data[-200_000:].decode("utf-8", "replace")
        return "\n".join(text.splitlines()[-lines:])


class BackgroundProcessManager:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.log_dir = project_root / ".trendlab" / "bg"
        self._procs: dict[str, BackgroundProcess] = {}

    def list(self) -> list[BackgroundProcess]:
        return list(self._procs.values())

    def get(self, ident: str) -> BackgroundProcess | None:
        if ident in self._procs:
            return self._procs[ident]
        return next((p for p in self._procs.values() if p.name == ident), None)

    async def start(
        self, command: str, *, cwd: Path, name: str | None = None, sandbox: Any = None
    ) -> BackgroundProcess:
        live = [p for p in self._procs.values() if p.running]
        if len(live) >= MAX_PROCESSES:
            raise RuntimeError(f"too many background processes ({MAX_PROCESSES}); stop one first")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        pid_id = secrets.token_hex(3)
        log_path = self.log_dir / f"{pid_id}.log"
        fh = open(log_path, "wb")  # noqa: SIM115 — closed in stop()/reap()
        if sandbox is not None and sandbox.active:
            argv = sandbox.wrap(command, cwd=cwd, allow_network=True)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdout=fh,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(cwd),
                stdout=fh,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        bp = BackgroundProcess(
            id=pid_id,
            name=name or command.split()[0][:24],
            command=command,
            cwd=cwd,
            log_path=log_path,
            started_at=time.monotonic(),
            proc=proc,
            _log_fh=fh,
        )
        self._procs[pid_id] = bp
        return bp

    async def stop(self, ident: str, *, timeout: float = 5.0) -> dict[str, Any] | None:
        bp = self.get(ident)
        if bp is None:
            return None
        if bp.running:
            try:
                os.killpg(bp.proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                bp.proc.terminate()
            try:
                await asyncio.wait_for(bp.proc.wait(), timeout=timeout)
            except TimeoutError:
                try:
                    os.killpg(bp.proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    bp.proc.kill()
                await bp.proc.wait()
        if bp._log_fh is not None:
            try:
                bp._log_fh.close()
            except OSError:
                pass
            bp._log_fh = None
        return bp.status()

    async def stop_all(self) -> None:
        for ident in list(self._procs):
            await self.stop(ident, timeout=2.0)


class BackgroundInput(BaseModel):
    action: str = Field(description="start | status | logs | stop | list")
    command: str | None = Field(default=None, description="Shell command (start)")
    name: str | None = Field(default=None, description="Friendly name for the process (start)")
    id: str | None = Field(default=None, description="Process id or name (status/logs/stop)")
    cwd: str | None = None
    lines: int = Field(default=60, ge=1, le=500, description="Log lines to return (logs)")
    explanation: str = ""


class BackgroundProcessTool(Tool):
    name = "background_process"
    description = (
        "Run a long-lived command in the background (dev server, watcher, long test run) and keep "
        "working. Actions: start(command,name) → id; status(id); logs(id,lines) tails its output; "
        "stop(id); list. Processes are killed when the session ends."
    )
    input_model = BackgroundInput

    def permission(self, args: BackgroundInput, ctx: ToolContext) -> PermissionRequest:
        cwd = str(ctx.resolve(args.cwd) if args.cwd else ctx.project_root)
        if args.action == "start":
            cat = classify_shell_command(args.command or "")
            if cat in {OperationCategory.SHELL_READ, OperationCategory.RUN_TESTS}:
                cat = OperationCategory.SHELL_WRITE  # a lingering process is more than a read
            return PermissionRequest(
                tool=self.name,
                category=cat,
                summary=f"Start background process: {(args.command or '')[:60]}",
                command=args.command,
                cwd=cwd,
                explanation=args.explanation[:300],
                args=args.model_dump(exclude={"explanation"}),
                task_id=ctx.task_id,
            )
        if args.action == "stop":
            return PermissionRequest(
                tool=self.name,
                category=OperationCategory.SHELL_WRITE,
                summary=f"Stop background process {args.id}",
                cwd=cwd,
                args=args.model_dump(exclude={"explanation"}),
                task_id=ctx.task_id,
            )
        return PermissionRequest(
            tool=self.name,
            category=OperationCategory.READ_ONLY,
            summary=f"Background process {args.action}" + (f" {args.id}" if args.id else ""),
            cwd=cwd,
            args=args.model_dump(exclude={"explanation"}),
            task_id=ctx.task_id,
        )

    async def run(self, args: BackgroundInput, ctx: ToolContext) -> ToolResult:
        mgr: BackgroundProcessManager | None = ctx.background
        if mgr is None:
            mgr = ctx.background = BackgroundProcessManager(ctx.project_root)
        action = args.action.lower()
        if action == "start":
            if not args.command:
                return ToolResult(ok=False, output="start needs a command")
            cwd = ctx.resolve(args.cwd) if args.cwd else ctx.project_root
            try:
                bp = await mgr.start(args.command, cwd=cwd, name=args.name, sandbox=ctx.sandbox)
            except RuntimeError as exc:
                return ToolResult(ok=False, output=str(exc))
            await asyncio.sleep(0.3)  # catch immediate failures (bad command, port in use)
            st = bp.status()
            head = (
                f"started {bp.id} ({bp.name}) pid {st['pid']}"
                if bp.running
                else f"{bp.id} exited immediately with code {st['exit_code']}"
            )
            tail = redact_text(bp.tail(20))
            return ToolResult(ok=bp.running, output=head + (f"\n{tail}" if tail else ""), data=st)
        if action == "list":
            rows = [p.status() for p in mgr.list()]
            text = "\n".join(
                f"{r['id']}  {r['name']:<16} "
                f"{('running' if r['running'] else 'exit ' + str(r['exit_code'])):<10} "
                f"{r['uptime_s']}s  {r['command'][:50]}"
                for r in rows
            )
            return ToolResult(
                ok=True, output=text or "no background processes", data={"processes": rows}
            )
        if not args.id:
            return ToolResult(ok=False, output=f"{action} needs an id")
        bp = mgr.get(args.id)
        if bp is None:
            return ToolResult(ok=False, output=f"no background process {args.id}")
        if action == "status":
            st = bp.status()
            return ToolResult(ok=True, output=redact_text(str(st)), data=st)
        if action == "logs":
            tail = redact_text(bp.tail(args.lines))
            return ToolResult(
                ok=True, output=tail or "(no output yet)", data={**bp.status(), "lines": args.lines}
            )
        if action == "stop":
            st = await mgr.stop(args.id)
            return ToolResult(
                ok=True, output=f"stopped {bp.id} (exit {st['exit_code']})", data=st or {}
            )
        return ToolResult(ok=False, output=f"unknown action {args.action}")
