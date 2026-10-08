"""Cost ledger and ROI (uplift U10).

Groups every model call by project (benchmark and engine scratch directories are their own
buckets, so evaluation spend is never mistaken for project work), then joins run outcomes:

* fully loaded cost: every role counts (lead, planner, verifier, router, summarizer, ...), and
  the share spent outside the lead model is reported as overhead;
* ROI: validated changes and completed runs per dollar, cost per validated change;
* waste: spend in sessions whose runs all failed or were canceled;
* token utilization: cache-hit share of input tokens, output/input ratio;
* period over period: this window against the one before it.

Budgets (``[economics]``) are checked month-to-date by ``budget_status`` and alerted once per
threshold by the engine.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

HEAVY_CALL_TOKENS = 30_000  # a prompt above this is worth a look (context stuffing)

BENCH_MARKERS = (
    "/trendlab-suite-",
    "/trendlab-plan-",
    "/trendlab-replay",
    "/trendlab-fixture",
    "/trendlab-bench-",
    "/tl-noisy-",
)
SCRATCH_ROOTS = ("/tmp/", "/var/tmp/")


def bucket(project_path: str | None) -> str:
    """Project key for a session: scratch benchmark dirs pool into '(benchmarks)'."""
    p = project_path or ""
    if not p:
        return "(unknown)"
    if any(m in p for m in BENCH_MARKERS):
        return "(benchmarks)"
    if p.rstrip("/") == str(Path.home()):
        return "~ (home)"
    if p.startswith(SCRATCH_ROOTS):
        return "(scratch)"  # throwaway directories: experiments, not project work
    return p


def _window(days: int, now: datetime | None) -> tuple[str, str, str]:
    now = now or datetime.now(UTC)
    start = now - timedelta(days=days)
    prev = start - timedelta(days=days)
    return prev.isoformat(), start.isoformat(), now.isoformat()


def ledger(store, days: int = 30, *, now: datetime | None = None) -> dict[str, Any]:
    prev_since, since, until = _window(days, now)
    calls = store.spend_rows(since, until)
    runs = store.run_outcomes(since, until)
    projects: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "cost": 0.0,
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "by_role": defaultdict(float),
            "by_model": defaultdict(float),
            "sessions": set(),
            "call_inputs": [],
            "heavy_cost": 0.0,
            "runs": 0,
            "completed": 0,
            "failed": 0,
            "validated_changes": 0,
        }
    )
    session_cost: dict[str, float] = defaultdict(float)
    for c in calls:
        p = projects[bucket(c["project_path"])]
        cost = float(c["cost_usd"] or 0.0)
        p["cost"] += cost
        p["calls"] += 1
        p["input_tokens"] += int(c["input_tokens"] or 0)
        p["output_tokens"] += int(c["output_tokens"] or 0)
        p["cached_tokens"] += int(c["cached_input_tokens"] or 0)
        p["by_role"][c["role"] or "main"] += cost
        inp = int(c["input_tokens"] or 0)
        p["call_inputs"].append(inp)
        if inp > HEAVY_CALL_TOKENS:
            p["heavy_cost"] += cost
        p["by_model"][c["model"]] += cost
        p["sessions"].add(c["session_id"])
        session_cost[c["session_id"]] += cost
    useful_sessions: set[str] = set()
    for r in runs:
        p = projects[bucket(r["project_path"])]
        p["runs"] += 1
        p["sessions"].add(r["session_id"])
        if r["outcome"] == "completed":
            p["completed"] += 1
            useful_sessions.add(r["session_id"])
        elif r["outcome"] == "failed":
            p["failed"] += 1
        p["validated_changes"] += 1 if r["validated"] else 0
    session_project = {}
    for c in calls:
        session_project[c["session_id"]] = bucket(c["project_path"])
    waste: dict[str, float] = defaultdict(float)
    ran = {r["session_id"] for r in runs}
    for sid, cost in session_cost.items():
        if sid in ran and sid not in useful_sessions:
            waste[session_project.get(sid, "(unknown)")] += cost
    rows = []
    for name, p in projects.items():
        lead = p["by_role"].get("main", 0.0) + p["by_role"].get("lead", 0.0)
        rows.append(
            {
                "project": name,
                "cost": round(p["cost"], 4),
                "sessions": len(p["sessions"]),
                "runs": p["runs"],
                "completed": p["completed"],
                "failed": p["failed"],
                "validated_changes": p["validated_changes"],
                "cost_per_run": round(p["cost"] / p["runs"], 4) if p["runs"] else None,
                "cost_per_validated_change": round(p["cost"] / p["validated_changes"], 4)
                if p["validated_changes"]
                else None,
                "roi_validated_per_usd": round(p["validated_changes"] / p["cost"], 1)
                if p["cost"]
                else None,
                "overhead_share": round(1 - lead / p["cost"], 3) if p["cost"] else None,
                "wasted": round(waste.get(name, 0.0), 4),
                "cache_share": round(p["cached_tokens"] / p["input_tokens"], 3)
                if p["input_tokens"]
                else None,
                # token maxing: how much spend goes to very large prompts
                "p95_input_tokens": sorted(p["call_inputs"])[
                    int(0.95 * (len(p["call_inputs"]) - 1))
                ]
                if p["call_inputs"]
                else None,
                "heavy_call_share": round(p["heavy_cost"] / p["cost"], 3) if p["cost"] else None,
                "by_role": {k: round(v, 4) for k, v in sorted(p["by_role"].items())},
                "by_model": {k: round(v, 4) for k, v in sorted(p["by_model"].items())},
            }
        )
    rows.sort(key=lambda r: -r["cost"])
    total = round(sum(r["cost"] for r in rows), 4)
    previous = round(sum(float(c["cost_usd"] or 0) for c in store.spend_rows(prev_since, since)), 4)
    work = [r for r in rows if not r["project"].startswith("(")]
    return {
        "days": days,
        "total": total,
        "previous_period": previous,
        "change": round((total - previous) / previous, 3) if previous else None,
        "work_share": round(sum(r["cost"] for r in work) / total, 3) if total else None,
        "wasted": round(sum(r["wasted"] for r in rows), 4),
        "projects": rows,
    }


def month_spend(store, *, now: datetime | None = None) -> dict[str, float]:
    """Month-to-date spend per project bucket, plus '*' for the total."""
    now = now or datetime.now(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    spent: dict[str, float] = defaultdict(float)
    for c in store.spend_rows(start.isoformat(), now.isoformat()):
        cost = float(c["cost_usd"] or 0.0)
        spent[bucket(c["project_path"])] += cost
        spent["*"] += cost
    return dict(spent)


def budget_status(store, econ, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Each configured budget with month-to-date spend and the thresholds it has crossed."""
    spent = month_spend(store, now=now)
    budgets: dict[str, float] = {}
    if econ.monthly_budget_usd:
        budgets["*"] = econ.monthly_budget_usd
    for path, usd in (econ.project_budgets or {}).items():
        budgets[bucket(str(Path(path).expanduser()))] = usd
    out = []
    for key, limit in budgets.items():
        used = spent.get(key, 0.0)
        share = used / limit if limit else 0.0
        out.append(
            {
                "budget": "all projects" if key == "*" else key,
                "key": key,
                "limit": limit,
                "spent": round(used, 4),
                "share": round(share, 3),
                "crossed": [t for t in sorted(econ.alert_at) if share >= t],
            }
        )
    return out


def new_alerts(
    status: list[dict[str, Any]], state: dict[str, Any], month: str
) -> list[dict[str, Any]]:
    """Threshold crossings not yet alerted this month; marks them in ``state``."""
    seen = state.setdefault("budget_alerts", {})
    if seen.get("month") != month:
        seen.clear()
        seen["month"] = month
    msgs = []
    for b in status:
        for t in b["crossed"]:
            key = f"{b['key']}@{t}"
            if key in seen:
                continue
            seen[key] = True
            msgs.append(
                {
                    "budget": b["budget"],
                    "threshold": t,
                    "text": f"TrendLab spend: {b['budget']} is at {b['share']:.0%} of its "
                    f"${b['limit']:.2f} monthly budget (${b['spent']:.2f} so far).",
                }
            )
    return msgs
