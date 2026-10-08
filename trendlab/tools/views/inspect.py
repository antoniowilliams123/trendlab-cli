"""``inspect_output``: pull a slice of a tiered tool result's full text (Tier 3) on demand."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from trendlab.permissions.engine import OperationCategory, PermissionRequest
from trendlab.tools.base import Tool, ToolContext, ToolResult

TRACE_DIR = ".trendlab/traces"
_CALL_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


def trace_path(project_root: Path, call_id: str) -> Path:
    return project_root / TRACE_DIR / f"{call_id}.log"


def write_trace(project_root: Path, call_id: str, text: str) -> str | None:
    """Store the full output; returns the project-relative path or None when it cannot."""
    if not _CALL_ID.match(call_id or ""):
        return None
    try:
        path = trace_path(project_root, call_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        _prune(path.parent)
    except OSError:
        return None
    return f"{TRACE_DIR}/{call_id}.log"


def _prune(folder: Path, keep: int = 200) -> None:
    files = sorted(folder.glob("*.log"), key=lambda p: p.stat().st_mtime)
    for old in files[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


class InspectOutputInput(BaseModel):
    call_id: str = Field(description="call_id shown in the '[full output: …]' pointer")
    query: str | None = Field(
        default=None, description="regex; returns matching lines with 2 lines of context"
    )
    lines: str | None = Field(
        default=None, description="line range to return, e.g. '120-180' (1-based, inclusive)"
    )
    max_chars: int = Field(default=4000, ge=200, le=20_000)


class InspectOutputTool(Tool):
    name = "inspect_output"
    description = (
        "Read part of an earlier tool result that was summarised. Give the call_id from the "
        "'[full output: …]' pointer plus either a regex `query` (matching lines + context) or a "
        "`lines` range such as '120-180'. Never returns more than max_chars."
    )
    input_model = InspectOutputInput

    def permission(self, args: InspectOutputInput, ctx: ToolContext) -> PermissionRequest:
        what = f"query {args.query!r}" if args.query else f"lines {args.lines or 'head'}"
        return PermissionRequest(
            tool=self.name,
            category=OperationCategory.READ_ONLY,
            summary=f"Inspect output {args.call_id} ({what})",
            cwd=str(ctx.project_root),
            args=args.model_dump(),
            task_id=ctx.task_id,
        )

    async def run(self, args: InspectOutputInput, ctx: ToolContext) -> ToolResult:
        if not _CALL_ID.match(args.call_id):
            return ToolResult(ok=False, output="invalid call_id")
        path = trace_path(ctx.project_root, args.call_id)
        if not path.is_file():
            return ToolResult(ok=False, output=f"no stored output for call {args.call_id}")
        text = path.read_text(encoding="utf-8", errors="replace")
        rows = text.splitlines()
        total = len(rows)
        if args.query:
            try:
                rx = re.compile(args.query, re.I)
            except re.error as exc:
                return ToolResult(ok=False, output=f"bad regex: {exc}")
            hits = [i for i, ln in enumerate(rows) if rx.search(ln)]
            if not hits:
                return ToolResult(
                    ok=True, output=f"0 of {total} lines match {args.query!r}", data={"matches": 0}
                )
            out: list[str] = [f"{len(hits)} of {total} lines match {args.query!r}"]
            shown: set[int] = set()
            for i in hits:
                for j in range(max(0, i - 2), min(total, i + 3)):
                    if j in shown:
                        continue
                    shown.add(j)
                    mark = ">" if j == i else " "
                    out.append(f"{j + 1:>6}{mark} {rows[j]}")
                if sum(len(x) + 1 for x in out) > args.max_chars:
                    out.append(f"… truncated at {args.max_chars} chars; narrow the query")
                    break
            body = "\n".join(out)
            return ToolResult(ok=True, output=body[: args.max_chars], data={"matches": len(hits)})
        start, end = 1, min(total, 200)
        if args.lines:
            m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", args.lines)
            if not m:
                return ToolResult(ok=False, output="lines must look like '120-180'")
            start, end = int(m.group(1)), min(int(m.group(2)), total)
            if start > end:
                return ToolResult(ok=False, output=f"range beyond end ({total} lines)")
        chunk = rows[start - 1 : end]
        body = "\n".join(f"{i:>6}  {ln}" for i, ln in enumerate(chunk, start=start))
        if len(body) > args.max_chars:
            body = body[: args.max_chars] + "\n… truncated; ask for a narrower range"
        header = f"lines {start}-{end} of {total}"
        return ToolResult(ok=True, output=f"{header}\n{body}", data={"total_lines": total})
