"""Controlled subprocess runner. The model never reaches ``subprocess`` directly:
its text goes through classification, the permission engine and (maybe) approval first."""

from __future__ import annotations

import asyncio
import os
import time

from pydantic import BaseModel, Field

from trendlab.permissions.classifier import classify_shell_command
from trendlab.permissions.engine import PermissionRequest
from trendlab.permissions.models import OperationCategory
from trendlab.security.redaction import redact_text
from trendlab.tools.base import PathOutsideProjectError, Tool, ToolContext, ToolResult

_SUMMARY = {
    OperationCategory.SHELL_READ: "Run read-only shell command",
    OperationCategory.RUN_TESTS: "Run tests / linters",
    OperationCategory.PACKAGE_INSTALL: "Install package(s)",
    OperationCategory.NETWORK: "Run network command",
    OperationCategory.SHELL_WRITE: "Run shell command",
    OperationCategory.DESTRUCTIVE: "Run DESTRUCTIVE command",
    OperationCategory.PRIVILEGED: "Run privileged command",
}

MAX_CAPTURE = 20_000


class ShellInput(BaseModel):
    command: str
    cwd: str | None = None
    timeout: int = Field(default=120, ge=1, le=3600)
    explanation: str = Field(
        default="", description="Why this command is needed (shown to the user)."
    )


class ShellTool(Tool):
    name = "shell"
    description = "Execute a shell command inside the project directory."
    input_model = ShellInput

    def permission(self, args: ShellInput, ctx: ToolContext) -> PermissionRequest:
        category = classify_shell_command(args.command)
        cwd = str(ctx.project_root)
        if args.cwd:
            try:
                cwd = str(ctx.resolve(args.cwd))
            except PathOutsideProjectError:
                category = OperationCategory.OUTSIDE_PROJECT
                cwd = args.cwd
        return PermissionRequest(
            tool=self.name,
            category=category,
            summary=_SUMMARY.get(category, "Run shell command"),
            command=args.command,
            cwd=cwd,
            explanation=args.explanation[:300],
            args=args.model_dump(exclude={"explanation"}),
            task_id=ctx.task_id,
        )

    async def run(self, args: ShellInput, ctx: ToolContext) -> ToolResult:
        cwd = ctx.resolve(args.cwd) if args.cwd else ctx.project_root
        env = {k: v for k, v in os.environ.items()}
        started = time.monotonic()
        proc = await asyncio.create_subprocess_shell(
            args.command,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=args.timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(
                ok=False,
                output=f"command timed out after {args.timeout}s",
                data={"exit_code": None, "timed_out": True},
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = _truncate(redact_text(stdout_b.decode("utf-8", "replace")))
        stderr = _truncate(redact_text(stderr_b.decode("utf-8", "replace")))
        output = stdout + (("\n[stderr]\n" + stderr) if stderr else "")
        return ToolResult(
            ok=proc.returncode == 0,
            output=output or f"(no output, exit {proc.returncode})",
            data={"exit_code": proc.returncode, "duration_ms": duration_ms},
        )


def _truncate(text: str) -> str:
    if len(text) <= MAX_CAPTURE:
        return text
    head, tail = text[: MAX_CAPTURE // 2], text[-MAX_CAPTURE // 2 :]
    return f"{head}\n... [{len(text) - MAX_CAPTURE} chars truncated] ...\n{tail}"
