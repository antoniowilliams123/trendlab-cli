"""Runtime glue for tiered tool output: budgets, baselines, screener hand-off, traces."""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from trendlab.tools.base import ToolResult
from trendlab.tools.views import ToolOutput, tier_output
from trendlab.tools.views.inspect import write_trace

CHARS_PER_TOKEN = 4
# Tools whose large results are tiered. read_file is deliberately absent: a file view must be
# exact (the model edits against it); its budget is enforced by the 400-line default range.
TIERED_TOOLS = {
    "shell",
    "run_tests",
    "search_text",
    "list_directory",
    "web_fetch",
    "git_diff",
    "git_log",
    "git_status",
    "glob",
}
HINTS = {
    "list_directory": "listing",
    "glob": "listing",
    "search_text": "search",
    "web_fetch": "http",
    "git_diff": "diff",
    "git_status": "status",
    "git_log": "log",
}
BASELINES_FILE = ".trendlab/baselines.json"

Screener = Callable[[str, dict[str, Any]], Awaitable[ToolOutput | None]]


class Baselines:
    """Remembers the last test count / duration per project so Tier 1 can flag anomalies."""

    def __init__(self, project_root: Path) -> None:
        self.path = project_root / BASELINES_FILE
        try:
            self.data: dict[str, Any] = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def check(self, out: ToolOutput) -> str | None:
        if out.kind != "tests":
            return None
        counts = out.stats.get("counts") or {}
        total = sum(v for k, v in counts.items() if k in {"passed", "failed", "errors", "skipped"})
        secs = out.stats.get("secs")
        prev = self.data.get("tests") or {}
        note = None
        if prev.get("total") and total and total < 0.8 * prev["total"]:
            note = f"anomaly: {total} tests ran, last time {prev['total']} (collection shrank?)"
        elif prev.get("secs") and secs and secs > 3 * prev["secs"] and secs > 5:
            note = f"anomaly: {secs:.0f}s, last time {prev['secs']:.0f}s"
        if total:
            self.data["tests"] = {"total": total, "secs": secs, "at": time.time()}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.data))
            except OSError:
                pass
        return note


def budget_chars(budgets: dict[str, int], tool: str) -> int | None:
    tokens = budgets.get(tool)
    return None if not tokens else tokens * CHARS_PER_TOKEN


def meta_for(tool: str, args: dict[str, Any], result: ToolResult, call_id: str) -> dict[str, Any]:
    meta: dict[str, Any] = {"tool": tool, "call_id": call_id}
    for k in ("exit_code", "duration_ms", "status", "url", "command"):
        if k in result.data:
            meta[k] = result.data[k]
    if tool == "shell":
        meta["command"] = args.get("command", "")
    if tool == "web_fetch":
        meta.setdefault("url", args.get("url", ""))
        meta["question"] = args.get("question") or args.get("query") or ""
    if tool in HINTS:
        meta["hint"] = HINTS[tool]
    return meta


async def tier_result(
    *,
    tool: str,
    args: dict[str, Any],
    result: ToolResult,
    call_id: str,
    project_root: Path,
    budgets: dict[str, int],
    screener: Screener | None,
    screener_threshold_tokens: int,
    baselines: Baselines | None,
) -> ToolOutput | None:
    """Replace ``result.output`` with a budgeted view when the raw text is over budget.

    Returns the ``ToolOutput`` used, or None when the result was left untouched.
    """
    if tool not in TIERED_TOOLS or not result.output:
        return None
    limit = budget_chars(budgets, tool)
    if limit is None or len(result.output) <= limit:
        return None
    raw = result.output
    meta = meta_for(tool, args, result, call_id)
    out = tier_output(raw, meta)
    if (
        out.parser == "generic"
        and screener is not None
        and len(raw) > screener_threshold_tokens * CHARS_PER_TOKEN
    ):
        try:
            screened = await screener(raw, meta)
        except Exception:  # noqa: BLE001 — the screener is best-effort
            screened = None
        if screened is not None and screened.tier1.strip():
            screened.parser = "screener"
            screened.stats.update({"lines": out.stats["lines"], "bytes": out.stats["bytes"]})
            out = screened
    if baselines is not None:
        note = baselines.check(out)
        if note:
            out.tier1 = f"{out.tier1}\n⚠ {note}"
            out.stats["anomaly"] = note
    out.tier3_ref = write_trace(project_root, call_id, raw)
    out.stats["call_id"] = call_id
    result.output = out.render(limit)
    result.data["tiers"] = out.model_dump()
    result.data["raw_chars"] = len(raw)
    return out
