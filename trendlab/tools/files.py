"""Read-only and mutating file tools with project-boundary enforcement."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from trendlab.permissions.engine import PermissionRequest
from trendlab.permissions.models import OperationCategory
from trendlab.tools.base import PathOutsideProjectError, Tool, ToolContext, ToolResult

_SENSITIVE = re.compile(
    r"(^|/)(\.env(\..*)?|id_rsa|id_ed25519|.*\.pem|.*\.key|credentials(\.json)?)$"
)


def _perm_or_outside(
    tool: str,
    ctx: ToolContext,
    raw: str,
    category: OperationCategory,
    summary: str,
    args: dict,
    affected: list[str] | None = None,
) -> PermissionRequest:
    try:
        resolved = ctx.resolve(raw)
        rel = str(resolved.relative_to(ctx.project_root.resolve()))
        if _SENSITIVE.search(rel.replace(os.sep, "/")):
            category = OperationCategory.OUTSIDE_PROJECT  # treat credential files like an escape
            summary = f"{summary} (sensitive file)"
    except PathOutsideProjectError:
        category = OperationCategory.OUTSIDE_PROJECT
        rel = raw
    return PermissionRequest(
        tool=tool,
        category=category,
        summary=summary,
        command=None,
        cwd=str(ctx.project_root),
        affected_files=affected if affected is not None else [rel],
        args=args,
        task_id=ctx.task_id,
    )


class ReadFileInput(BaseModel):
    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a file inside the project (optionally a line range)."
    input_model = ReadFileInput

    def permission(self, args: ReadFileInput, ctx: ToolContext) -> PermissionRequest:
        return _perm_or_outside(
            self.name,
            ctx,
            args.path,
            OperationCategory.READ_ONLY,
            f"Read {args.path}",
            args.model_dump(),
        )

    async def run(self, args: ReadFileInput, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(args.path)
        if not path.is_file():
            return ToolResult(ok=False, output=f"not a file: {args.path}")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        end = args.end_line or min(len(lines), args.start_line + 399)
        chunk = lines[args.start_line - 1 : end]
        numbered = "\n".join(f"{i}\t{line}" for i, line in enumerate(chunk, start=args.start_line))
        return ToolResult(ok=True, output=numbered, data={"total_lines": len(lines)})


class ListDirectoryInput(BaseModel):
    path: str = "."


class ListDirectoryTool(Tool):
    name = "list_directory"
    description = "List entries of a directory inside the project."
    input_model = ListDirectoryInput

    def permission(self, args: ListDirectoryInput, ctx: ToolContext) -> PermissionRequest:
        return _perm_or_outside(
            self.name,
            ctx,
            args.path,
            OperationCategory.READ_ONLY,
            f"List {args.path}",
            args.model_dump(),
        )

    async def run(self, args: ListDirectoryInput, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(args.path)
        if not path.is_dir():
            return ToolResult(ok=False, output=f"not a directory: {args.path}")
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        out = "\n".join(f"{e.name}/" if e.is_dir() else e.name for e in entries[:500])
        return ToolResult(ok=True, output=out)


class SearchTextInput(BaseModel):
    pattern: str
    path: str = "."
    max_results: int = Field(default=100, ge=1, le=1000)


class SearchTextTool(Tool):
    name = "search_text"
    description = "Search for a regular expression in project files."
    input_model = SearchTextInput

    def permission(self, args: SearchTextInput, ctx: ToolContext) -> PermissionRequest:
        return _perm_or_outside(
            self.name,
            ctx,
            args.path,
            OperationCategory.READ_ONLY,
            f"Search for /{args.pattern}/",
            args.model_dump(),
        )

    async def run(self, args: SearchTextInput, ctx: ToolContext) -> ToolResult:
        root = ctx.resolve(args.path)
        try:
            regex = re.compile(args.pattern)
        except re.error as exc:
            return ToolResult(ok=False, output=f"invalid regex: {exc}")
        hits: list[str] = []
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
        for file in files:
            if any(part in {".git", ".venv", "node_modules", "__pycache__"} for part in file.parts):
                continue
            try:
                text = file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    rel = file.relative_to(ctx.project_root.resolve())
                    hits.append(f"{rel}:{n}: {line.strip()[:200]}")
                    if len(hits) >= args.max_results:
                        return ToolResult(ok=True, output="\n".join(hits), data={"truncated": True})
        return ToolResult(ok=True, output="\n".join(hits) or "no matches")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class WriteFileInput(BaseModel):
    path: str
    content: str
    expected_sha256: str | None = Field(
        default=None,
        description="Hash of the current file content; write is refused if it differs.",
    )


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Create or overwrite a project file atomically. "
        "Pass expected_sha256 to guard against external edits."
    )
    input_model = WriteFileInput

    def permission(self, args: WriteFileInput, ctx: ToolContext) -> PermissionRequest:
        return _perm_or_outside(
            self.name,
            ctx,
            args.path,
            OperationCategory.PROJECT_WRITE,
            f"Write {args.path} ({len(args.content)} chars)",
            args.model_dump(),
        )

    async def run(self, args: WriteFileInput, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(args.path)
        if path.exists():
            current = path.read_bytes()
            if args.expected_sha256 and _sha256(current) != args.expected_sha256:
                return ToolResult(ok=False, output="conflict: file changed since it was read")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".trendlab-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(args.content)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return ToolResult(
            ok=True, output=f"wrote {args.path}", data={"sha256": _sha256(args.content.encode())}
        )


class DeleteFileInput(BaseModel):
    path: str


class DeleteFileTool(Tool):
    name = "delete_file"
    description = "Delete a single project file."
    input_model = DeleteFileInput

    def permission(self, args: DeleteFileInput, ctx: ToolContext) -> PermissionRequest:
        return _perm_or_outside(
            self.name,
            ctx,
            args.path,
            OperationCategory.FILE_DELETE,
            f"Delete {args.path}",
            args.model_dump(),
        )

    async def run(self, args: DeleteFileInput, ctx: ToolContext) -> ToolResult:
        path: Path = ctx.resolve(args.path)
        if not path.is_file():
            return ToolResult(ok=False, output=f"not a file: {args.path}")
        path.unlink()
        return ToolResult(ok=True, output=f"deleted {args.path}")
