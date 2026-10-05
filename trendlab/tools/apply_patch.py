"""``apply_patch``: apply a unified diff to project files (spec §15 "Generate and apply structured
file patches"). Complements ``patch_file`` (exact text) for multi-hunk, multi-file edits.

Hunks are matched by context with a small positional fuzz; a hunk whose context does not match
anywhere is rejected and nothing is written (all-or-nothing per call). New files (``--- /dev/null``)
and deletions (``+++ /dev/null``) are supported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from trendlab.permissions.engine import PermissionRequest
from trendlab.permissions.models import OperationCategory
from trendlab.tools.base import PathOutsideProjectError, Tool, ToolContext, ToolResult
from trendlab.tools.files import _atomic_write, _perm_or_outside, _sha256
from trendlab.ui.diff_view import diff_stats, unified_diff

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(ValueError):
    pass


@dataclass
class Hunk:
    old_start: int
    lines: list[str] = field(default_factory=list)  # with leading ' ', '-', '+'


@dataclass
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)


def _strip_prefix(path: str) -> str | None:
    path = path.strip()
    if path in {"/dev/null", ""}:
        return None
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def parse_unified_diff(text: str) -> list[FilePatch]:
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    for raw in text.splitlines():
        if raw.startswith("--- "):
            current = FilePatch(old_path=_strip_prefix(raw[4:]), new_path=None)
            patches.append(current)
            hunk = None
        elif raw.startswith("+++ ") and current is not None:
            current.new_path = _strip_prefix(raw[4:])
        elif raw.startswith("@@") and current is not None:
            m = _HUNK.match(raw)
            if not m:
                raise PatchError(f"bad hunk header: {raw}")
            hunk = Hunk(old_start=int(m.group(1)))
            current.hunks.append(hunk)
        elif hunk is not None and raw[:1] in {" ", "-", "+", "\\"}:
            if raw.startswith("\\"):
                continue  # "\ No newline at end of file"
            hunk.lines.append(raw)
        elif hunk is not None and raw == "":
            hunk.lines.append(" ")  # blank context line with trailing space stripped
    if not patches:
        raise PatchError("no file headers (--- a/... / +++ b/...) found")
    for p in patches:
        if p.old_path is None and p.new_path is None:
            raise PatchError("patch with neither old nor new path")
        if not p.hunks and not (p.old_path and p.new_path is None):
            raise PatchError(f"no hunks for {p.new_path or p.old_path}")
    return patches


def apply_hunks(original: str, hunks: list[Hunk], *, fuzz: int = 50) -> str:
    lines = original.splitlines()
    out: list[str] = []
    pos = 0  # index into lines already emitted
    offset = 0
    for h in hunks:
        old_block = [ln[1:] for ln in h.lines if ln[0] in " -"]
        new_block = [ln[1:] for ln in h.lines if ln[0] in " +"]
        guess = max(0, h.old_start - 1 + offset)
        start = _locate(lines, old_block, guess, fuzz, floor=pos)
        if start is None:
            ctx = "\n".join(old_block[:3])
            raise PatchError(f"hunk at line {h.old_start} does not match the file; context:\n{ctx}")
        out.extend(lines[pos:start])
        out.extend(new_block)
        pos = start + len(old_block)
        offset += len(new_block) - len(old_block) + (start - (h.old_start - 1 + offset))
    out.extend(lines[pos:])
    text = "\n".join(out)
    if original.endswith("\n") or not original:
        text += "\n"
    return text


def _locate(lines: list[str], block: list[str], guess: int, fuzz: int, *, floor: int) -> int | None:
    if not block:
        return min(max(guess, floor), len(lines))
    n = len(block)

    def matches(i: int) -> bool:
        return i >= floor and i + n <= len(lines) and lines[i : i + n] == block

    if matches(guess):
        return guess
    for d in range(1, fuzz + 1):
        if matches(guess - d):
            return guess - d
        if matches(guess + d):
            return guess + d
    # Whitespace-insensitive last resort.
    stripped = [b.strip() for b in block]
    for i in range(floor, len(lines) - n + 1):
        if [ln.strip() for ln in lines[i : i + n]] == stripped:
            return i
    return None


class ApplyPatchInput(BaseModel):
    diff: str = Field(
        description="A unified diff (--- a/path / +++ b/path / @@ hunks). May span files."
    )
    explanation: str = Field(
        default="", description="Why this change is needed (shown to the approver)."
    )


class ApplyPatchTool(Tool):
    name = "apply_patch"
    description = (
        "Apply a unified diff to one or more project files atomically (all hunks must match or "
        "nothing is written). Supports new files (--- /dev/null) and deletions (+++ /dev/null). "
        "Use for multi-hunk or multi-file edits; use patch_file for a single exact replacement."
    )
    input_model = ApplyPatchInput

    def _plan(
        self, args: ApplyPatchInput, ctx: ToolContext
    ) -> list[tuple[str, str | None, str | None]]:
        """Return [(rel_path, old_content, new_content)]; None old = create, None new = delete."""
        plan = []
        for fp in parse_unified_diff(args.diff):
            rel = fp.new_path or fp.old_path
            assert rel is not None
            target = ctx.resolve(rel)
            if fp.new_path is None:  # deletion
                old = (
                    target.read_text(encoding="utf-8", errors="replace")
                    if target.is_file()
                    else None
                )
                if old is None:
                    raise PatchError(f"cannot delete {rel}: not a file")
                plan.append((rel, old, None))
                continue
            old = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
            if fp.old_path is None and old:
                raise PatchError(f"{rel} already exists but the patch creates it")
            new = apply_hunks(old, fp.hunks)
            plan.append((rel, old if target.is_file() else None, new))
        return plan

    def permission(self, args: ApplyPatchInput, ctx: ToolContext) -> PermissionRequest:
        try:
            plan = self._plan(args, ctx)
        except (PatchError, PathOutsideProjectError) as exc:
            if isinstance(exc, PathOutsideProjectError):
                return PermissionRequest(
                    tool=self.name,
                    category=OperationCategory.OUTSIDE_PROJECT,
                    summary="Patch outside project",
                    cwd=str(ctx.project_root),
                    args=args.model_dump(exclude={"explanation"}),
                    task_id=ctx.task_id,
                )
            return PermissionRequest(
                tool=self.name,
                category=OperationCategory.PROJECT_WRITE,
                summary="Apply patch (will fail)",
                cwd=str(ctx.project_root),
                preview=f"(patch would fail: {exc})",
                explanation=args.explanation[:300],
                args=args.model_dump(exclude={"explanation"}),
                task_id=ctx.task_id,
            )
        previews, files, added, removed = [], [], 0, 0
        category = OperationCategory.PROJECT_WRITE
        for rel, old, new in plan:
            d = unified_diff(rel, old or "", new or "")
            a, r = diff_stats(d)
            added, removed = added + a, removed + r
            previews.append(d)
            files.append(rel)
            if new is None:
                category = OperationCategory.FILE_DELETE
        req = _perm_or_outside(
            self.name,
            ctx,
            files[0],
            category,
            f"Apply patch to {len(files)} file(s) (+{added} -{removed})",
            args.model_dump(exclude={"explanation"}),
            affected=files,
            preview="\n".join(previews),
            explanation=args.explanation[:300],
        )
        return req

    async def run(self, args: ApplyPatchInput, ctx: ToolContext) -> ToolResult:
        try:
            plan = self._plan(args, ctx)
        except PatchError as exc:
            return ToolResult(ok=False, output=f"patch failed (nothing written): {exc}")
        diffs = []
        for rel, old, new in plan:
            target = ctx.resolve(rel)
            if new is None:
                target.unlink()
            else:
                _atomic_write(target, new)
            diffs.append(unified_diff(rel, old or "", new or ""))
        full = "\n".join(diffs)
        added, removed = diff_stats(full)
        return ToolResult(
            ok=True,
            output=f"applied patch to {len(plan)} file(s) (+{added} -{removed})\n{full}",
            data={
                "diff": full,
                "files": [p[0] for p in plan],
                "sha256": {p[0]: _sha256(p[2].encode()) for p in plan if p[2] is not None},
            },
        )
