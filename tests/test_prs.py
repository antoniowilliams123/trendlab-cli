"""Uplift U21: PR triage, throughput, PR workspace."""

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trendlab.orchestration.prs import ci_state, list_open, throughput, triage, workspace

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)


def _pr(
    n, *, adds=10, dels=0, draft=False, review="REVIEW_REQUIRED", checks=("SUCCESS",), idle=0, age=1
):
    return {
        "number": n,
        "title": f"pr {n}",
        "author": {"login": "a"},
        "additions": adds,
        "deletions": dels,
        "changedFiles": 1,
        "isDraft": draft,
        "reviewDecision": review,
        "statusCheckRollup": [{"conclusion": c} for c in checks],
        "createdAt": (NOW - timedelta(days=age)).isoformat(),
        "updatedAt": (NOW - timedelta(days=idle)).isoformat(),
        "headRefName": f"b{n}",
    }


def test_ci_state():
    assert ci_state([]) == "none"
    assert ci_state([{"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}]) == "passing"
    assert ci_state([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]) == "failing"
    assert ci_state([{"status": "IN_PROGRESS"}]) == "pending"


def test_triage_orders_the_quickest_unblocking_work_first():
    prs = [
        _pr(1, adds=1500, review="REVIEW_REQUIRED"),
        _pr(2, adds=20),
        _pr(3, review="APPROVED"),
        _pr(4, checks=("FAILURE",)),
        _pr(5, draft=True),
        _pr(6, idle=30, age=40),
        _pr(7, review="CHANGES_REQUESTED"),
        _pr(8, checks=("PENDING",)),
    ]
    rows = triage(prs, now=NOW)
    assert [r["number"] for r in rows] == [3, 2, 1, 4, 7, 8, 6, 5]
    by = {r["number"]: r for r in rows}
    assert by[3]["action"] == "ready to merge" and by[1]["note"] == "consider splitting"
    assert by[1]["size"] == "XL" and by[2]["size"] == "S" and by[6]["action"] == "stale"


def test_throughput():
    merged = [
        {
            "createdAt": (NOW - timedelta(hours=h + 1)).isoformat(),
            "mergedAt": (NOW - timedelta(hours=1)).isoformat(),
            "additions": a,
            "deletions": 0,
        }
        for h, a in ((2, 10), (10, 30), (50, 200))
    ]
    t = throughput(merged, days=7)
    assert t["merged"] == 3 and t["per_week"] == 3.0
    assert t["median_hours_to_merge"] == 10.0 and t["median_lines"] == 30


def test_list_open_uses_gh_json():
    seen = []

    def fake(args, cwd):
        seen.append(args)
        return json.dumps([_pr(1)])

    assert list_open("o/r", None, 5, run=fake)[0]["number"] == 1
    assert seen[0][:3] == ["pr", "list", "--state"] and "--repo" in seen[0]


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout


def test_workspace_checks_a_pr_out_beside_the_current_checkout(tmp_path: Path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    (work / "a.txt").write_text("main\n")
    _git(work, "add", "-A")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    (work / "a.txt").write_text("pr change\n")
    _git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "pr")
    _git(work, "push", "-q", "origin", "HEAD:refs/pull/7/head")
    _git(work, "reset", "-q", "--hard", "HEAD~1")
    dest = workspace(work, 7)
    assert (dest / "a.txt").read_text() == "pr change\n"
    assert (work / "a.txt").read_text() == "main\n"  # current checkout untouched
    assert workspace(work, 7) == dest  # idempotent
