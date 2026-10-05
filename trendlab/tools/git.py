"""Read-only Git tools behind a controlled adapter (spec §26).

Commits are a slash command, never a model-callable tool."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from trendlab.permissions.engine import PermissionRequest
from trendlab.permissions.models import OperationCategory
from trendlab.tools.base import Tool, ToolContext, ToolResult


async def run_git(root: Path, *args: str, timeout: int = 30) -> tuple[int, str]:
    if shutil.which("git") is None:
        return 127, "git is not installed"
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return 124, "git timed out"
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def git_branch(root: Path) -> str | None:
    code, out = await run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return out.strip() if code == 0 else None


async def git_head(root: Path) -> str | None:
    code, out = await run_git(root, "rev-parse", "HEAD")
    return out.strip() if code == 0 else None


def _perm(tool: str, ctx: ToolContext, summary: str, args: dict) -> PermissionRequest:
    return PermissionRequest(
        tool=tool,
        category=OperationCategory.READ_ONLY,
        summary=summary,
        command=f"git {tool[4:]}",
        cwd=str(ctx.project_root),
        args=args,
        task_id=ctx.task_id,
    )


class GitStatusInput(BaseModel):
    pass


class GitStatusTool(Tool):
    name = "git_status"
    description = "Show the Git working-tree status (short format) and current branch."
    input_model = GitStatusInput

    def permission(self, args, ctx):
        return _perm(self.name, ctx, "Git status", {})

    async def run(self, args, ctx: ToolContext) -> ToolResult:
        code, out = await run_git(ctx.project_root, "status", "--short", "--branch")
        return ToolResult(ok=code == 0, output=out.strip() or "clean", data={"exit_code": code})


class GitDiffInput(BaseModel):
    path: str | None = None
    staged: bool = False
    stat_only: bool = False


class GitDiffTool(Tool):
    name = "git_diff"
    description = "Show uncommitted changes (optionally one path, staged only, or --stat)."
    input_model = GitDiffInput

    def permission(self, args: GitDiffInput, ctx):
        return _perm(self.name, ctx, "Git diff", args.model_dump())

    async def run(self, args: GitDiffInput, ctx: ToolContext) -> ToolResult:
        cmd = ["diff"]
        if args.staged:
            cmd.append("--cached")
        if args.stat_only:
            cmd.append("--stat")
        if args.path:
            cmd += ["--", str(ctx.resolve(args.path).relative_to(ctx.project_root.resolve()))]
        code, out = await run_git(ctx.project_root, *cmd)
        if len(out) > 30_000:
            out = out[:30_000] + "\n... diff truncated"
        return ToolResult(
            ok=code == 0, output=out.strip() or "no changes", data={"exit_code": code}
        )


class GitLogInput(BaseModel):
    max_count: int = Field(default=15, ge=1, le=200)
    path: str | None = None


class GitLogTool(Tool):
    name = "git_log"
    description = "Show recent commits (one line each)."
    input_model = GitLogInput

    def permission(self, args: GitLogInput, ctx):
        return _perm(self.name, ctx, "Git log", args.model_dump())

    async def run(self, args: GitLogInput, ctx: ToolContext) -> ToolResult:
        cmd = [
            "log",
            f"--max-count={args.max_count}",
            "--date=short",
            "--pretty=format:%h %ad %an  %s",
        ]
        if args.path:
            cmd += ["--", str(ctx.resolve(args.path).relative_to(ctx.project_root.resolve()))]
        code, out = await run_git(ctx.project_root, *cmd)
        return ToolResult(
            ok=code == 0, output=out.strip() or "no commits", data={"exit_code": code}
        )
