"""run_tests: execute the project's configured/detected validation commands (spec §15 M15, §66)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from trendlab.context.validation import KINDS
from trendlab.permissions.engine import PermissionRequest
from trendlab.permissions.models import OperationCategory
from trendlab.tools.base import Tool, ToolContext, ToolResult
from trendlab.tools.shell import ShellInput, ShellTool


class RunTestsInput(BaseModel):
    kind: str = Field(default="test", description="test | lint | typecheck | build")
    extra_args: str = Field(
        default="", description="Appended to the configured command, e.g. 'tests/test_x.py -x'"
    )
    timeout: int = Field(default=600, ge=1, le=3600)


class RunTestsTool(Tool):
    name = "run_tests"
    description = (
        "Run the project's validation command (test/lint/typecheck/build) as detected from the "
        "project or configured in [project]. Output is the evidence used to judge completion."
    )
    input_model = RunTestsInput

    def __init__(self) -> None:
        self._shell = ShellTool()

    def _command(self, args: RunTestsInput, ctx: ToolContext) -> str | None:
        cmd = (ctx.validation_commands or {}).get(args.kind)
        if not cmd:
            return None
        return f"{cmd} {args.extra_args}".strip()

    def permission(self, args: RunTestsInput, ctx: ToolContext) -> PermissionRequest:
        cmd = self._command(args, ctx) or f"(no {args.kind} command configured)"
        return PermissionRequest(
            tool=self.name,
            category=OperationCategory.RUN_TESTS,
            summary=f"Run {args.kind}: {cmd}",
            command=cmd,
            cwd=str(ctx.project_root),
            args=args.model_dump(),
            task_id=ctx.task_id,
        )

    async def run(self, args: RunTestsInput, ctx: ToolContext) -> ToolResult:
        if args.kind not in KINDS:
            return ToolResult(ok=False, output=f"kind must be one of {', '.join(KINDS)}")
        cmd = self._command(args, ctx)
        if cmd is None:
            known = (
                ", ".join(f"{k}={v}" for k, v in (ctx.validation_commands or {}).items()) or "none"
            )
            return ToolResult(
                ok=False,
                output=f"no {args.kind} command configured (known: {known}); "
                f"use the shell tool or set [project] {args.kind}_command",
            )
        result = await self._shell.run(ShellInput(command=cmd, timeout=args.timeout), ctx)
        result.data["validation_kind"] = args.kind
        result.data["command"] = cmd
        return result
