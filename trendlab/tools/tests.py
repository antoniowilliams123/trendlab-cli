"""run_tests: execute the project's configured/detected validation commands (spec §15 M15, §66)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from trendlab.context.validation import KINDS, detect_validation_commands, nearest_project
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
    path: str = Field(
        default="",
        description="Folder of the project to test (default: the project around the files "
        "changed this session, else the project root)",
    )


class RunTestsTool(Tool):
    name = "run_tests"
    description = (
        "Run the project's validation command (test/lint/typecheck/build) as detected from the "
        "project or configured in [project]. Output is the evidence used to judge completion."
    )
    input_model = RunTestsInput

    def __init__(self) -> None:
        self._shell = ShellTool()

    def _where(self, args: RunTestsInput, ctx: ToolContext) -> Path:
        """The project to test: an explicit path, else the one around this session's edits."""
        root = ctx.project_root.resolve()
        if args.path:
            try:
                d = ctx.resolve(args.path)
            except Exception:  # noqa: BLE001 — outside the project: test the root
                return root
            return d if d.is_dir() else d.parent
        return nearest_project(root, list(ctx.changed_files or {}))

    def _command(self, args: RunTestsInput, ctx: ToolContext) -> str | None:
        where = self._where(args, ctx)
        root = ctx.project_root.resolve()
        configured = (ctx.validation_commands or {}).get(args.kind)
        if where != root:
            # a sub-project's own command (configured [project] commands are for the root)
            cmd = detect_validation_commands(where).get(args.kind)
        else:
            cmd = configured
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
            cwd=str(self._where(args, ctx)),
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
                output=f"no {args.kind} command for {self._where(args, ctx).name or '.'} "
                f"(known: {known}); pass path=<folder> to test a sub-project, use the shell "
                f"tool, or set [project] {args.kind}_command",
            )
        where = self._where(args, ctx)
        root = ctx.project_root.resolve()
        rel = "" if where == root else where.relative_to(root).as_posix()
        result = await self._shell.run(
            ShellInput(command=cmd, timeout=args.timeout, cwd=rel or None), ctx
        )
        if rel:
            result.output = f"(ran in {rel}/)\n{result.output}"
        result.data["validation_kind"] = args.kind
        result.data["command"] = cmd
        result.data["cwd"] = rel or "."
        return result
