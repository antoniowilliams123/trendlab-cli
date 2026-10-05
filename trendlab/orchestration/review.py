"""``/review``: run the reviewer sub-agent over the current diff (spec §65)."""

from __future__ import annotations

from trendlab.orchestration.subagents import SubAgentTask, render_report
from trendlab.tools.git import run_git


async def run_review(app) -> str:
    runner = getattr(app, "subagents", None)
    if runner is None:
        return "review agent is not available in this session"
    diff_parts = []
    if app.tools and app.tools.changed_files:
        for diffs in app.tools.changed_files.values():
            diff_parts.extend(d for d in diffs if d)
    if not diff_parts:
        code, out = await run_git(app.project_root, "diff")
        if code == 0 and out.strip():
            diff_parts.append(out)
    if not diff_parts:
        return "nothing to review: no changes this session and no uncommitted git diff"
    diff = "\n".join(diff_parts)[:40_000]
    task = SubAgentTask(
        role="reviewer",
        objective="Review the following diff for defects, regressions, "
        "security issues, missing tests and unnecessary changes.",
        context=f"DIFF:\n{diff}",
    )
    report = await runner.run(task)
    return render_report(report)
