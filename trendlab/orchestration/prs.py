"""Pull-request workflow (uplift U21): triage, throughput, a workspace per PR.

Everything goes through the ``gh`` CLI (already authenticated on the machine), read-only except
``workspace``, which only creates a local git worktree. Nothing is posted to GitHub.

* ``triage``: every open PR gets a size bucket, CI state, review state, age and idle time, and
  one next action — ready to merge, needs review, fix CI, waiting on the author, stale, draft —
  ordered so the quickest unblocking work comes first (small reviews before big ones).
* ``throughput``: merged PRs per week, hours to merge (median and p90), size of what merged.
* ``workspace``: fetch a PR into its own worktree so the agent (or ``trendlab review``) can
  work on it without touching the current checkout.
"""

from __future__ import annotations

import json
import statistics
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FIELDS = (
    "number,title,author,additions,deletions,changedFiles,createdAt,updatedAt,isDraft,"
    "reviewDecision,statusCheckRollup,headRefName,url"
)
Runner = Callable[[list[str], Path | None], str]


def gh(args: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(
        ["gh", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=120
    ).stdout


def _when(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def ci_state(checks: list[dict[str, Any]] | None) -> str:
    if not checks:
        return "none"
    states = {
        str(c.get("conclusion") or c.get("state") or c.get("status") or "").upper() for c in checks
    }
    if states & {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}:
        return "failing"
    if states & {"PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "WAITING", ""}:
        return "pending"
    return "passing"


def size_bucket(lines: int) -> str:
    for limit, name in ((10, "XS"), (50, "S"), (250, "M"), (1000, "L")):
        if lines < limit:
            return name
    return "XL"


ORDER = {
    "ready to merge": 0,
    "needs review": 1,
    "fix CI": 2,
    "waiting on author": 3,
    "wait for CI": 4,
    "stale": 5,
    "draft": 6,
}


def triage(
    prs: list[dict[str, Any]], *, now: datetime | None = None, stale_days: int = 14
) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    rows = []
    for p in prs:
        lines = int(p.get("additions") or 0) + int(p.get("deletions") or 0)
        created, updated = _when(p.get("createdAt")), _when(p.get("updatedAt"))
        age = (now - created).days if created else None
        idle = (now - updated).days if updated else None
        ci = ci_state(p.get("statusCheckRollup"))
        review = str(p.get("reviewDecision") or "NONE")
        if p.get("isDraft"):
            action = "draft"
        elif ci == "failing":
            action = "fix CI"
        elif review == "CHANGES_REQUESTED":
            action = "waiting on author"
        elif idle is not None and idle >= stale_days:
            action = "stale"
        elif ci == "pending":
            action = "wait for CI"
        elif review == "APPROVED":
            action = "ready to merge"
        else:
            action = "needs review"
        bucket = size_bucket(lines)
        rows.append(
            {
                "number": p.get("number"),
                "title": str(p.get("title") or "")[:90],
                "author": (p.get("author") or {}).get("login", ""),
                "lines": lines,
                "files": p.get("changedFiles"),
                "size": bucket,
                "ci": ci,
                "review": review,
                "age_days": age,
                "idle_days": idle,
                "action": action,
                "note": "consider splitting" if bucket == "XL" and action == "needs review" else "",
                "branch": p.get("headRefName"),
            }
        )
    rows.sort(key=lambda r: (ORDER[r["action"]], r["lines"]))
    return rows


def throughput(merged: list[dict[str, Any]], *, days: int) -> dict[str, Any]:
    hours = []
    sizes = []
    for p in merged:
        c, m = _when(p.get("createdAt")), _when(p.get("mergedAt"))
        if c and m:
            hours.append((m - c).total_seconds() / 3600)
        sizes.append(int(p.get("additions") or 0) + int(p.get("deletions") or 0))
    hours.sort()
    return {
        "days": days,
        "merged": len(merged),
        "per_week": round(len(merged) / max(1, days) * 7, 2),
        "median_hours_to_merge": round(statistics.median(hours), 1) if hours else None,
        "p90_hours_to_merge": round(hours[int(0.9 * (len(hours) - 1))], 1) if hours else None,
        "median_lines": statistics.median(sizes) if sizes else None,
    }


def list_open(repo: str | None, cwd: Path | None, limit: int = 50, run: Runner = gh):
    args = ["pr", "list", "--state", "open", "--limit", str(limit), "--json", FIELDS]
    if repo:
        args += ["--repo", repo]
    return json.loads(run(args, cwd) or "[]")


def list_merged(repo: str | None, cwd: Path | None, days: int, run: Runner = gh):
    since = datetime.now(UTC).date().toordinal() - days
    since_date = datetime.fromordinal(since).date().isoformat()
    args = [
        "pr",
        "list",
        "--state",
        "merged",
        "--limit",
        "500",
        "--search",
        f"merged:>={since_date}",
        "--json",
        "number,createdAt,mergedAt,additions,deletions",
    ]
    if repo:
        args += ["--repo", repo]
    return json.loads(run(args, cwd) or "[]")


def workspace(root: Path, number: int, *, run_git=None) -> Path:
    """Fetch PR ``number`` into its own worktree under .trendlab/pr-worktrees/; the current
    checkout is untouched. Returns the worktree path."""

    def git(*args):
        if run_git is not None:
            return run_git(*args)
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
        ).stdout

    dest = root / ".trendlab" / "pr-worktrees" / f"pr-{number}"
    branch = f"trendlab/pr-{number}"
    if dest.exists():
        # already checked out: never reset it (it may hold the agent's work); just refresh
        # FETCH_HEAD so the newest PR head can be compared or merged deliberately
        git("fetch", "origin", f"pull/{number}/head")
        return dest
    git("fetch", "origin", f"pull/{number}/head:{branch}", "--force")
    git("worktree", "add", str(dest), branch)
    return dest
