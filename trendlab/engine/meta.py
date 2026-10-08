"""Meta-loop (cheap-model spec §7.4): the harness screens its own sessions.

Clusters stop reasons, skipped tools, guards, loop detections, verifier fails and cost
outliers across stored sessions, files inbox cards *against the harness* with session ids as
evidence, and (optionally) drafts a fix for the top card in a worktree of trendlab-cli itself.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trendlab.engine.inbox import Inbox

HARNESS_PROJECT = "harness:trendlab-cli"
SIGNALS = {
    "run.failed": lambda d: ("stop_reason", str(d.get("stop_reason") or "")[:120]),
    "tool.skipped": lambda d: ("tool_skipped", f"{d.get('tool')}:{d.get('reason')}"),
    "guard.fired": lambda d: ("guard", f"{d.get('guard')}@{d.get('model')}"),
    "loop.detected": lambda d: ("loop", f"{d.get('tool')}:{str(d.get('reason') or '')[:60]}"),
    "verify.verdict": lambda d: (
        ("verifier_fail", str(d.get("model") or "")) if d.get("verdict") == "fail" else None
    ),
}


def scan(store, *, days: int = 7, min_count: int = 2) -> list[dict[str, Any]]:
    """Clusters over the last ``days`` of sessions, biggest first."""
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    clusters: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "sessions": set(), "projects": set()}
    )
    costs: list[tuple[float, str, str]] = []
    for sess in store.sessions(limit=400):
        if sess.get("updated_at", "") < since:
            continue
        sid, project = sess["id"], sess.get("project_path", "")
        try:
            usage = store.usage(sid)
            costs.append((float(usage.get("cost_usd") or 0.0), sid, project))
        except Exception:  # noqa: BLE001
            pass
        for ev in store.events(sid):
            fn = SIGNALS.get(ev.get("type", ""))
            if fn is None:
                continue
            key = fn(ev.get("data") or {})
            if key is None:
                continue
            c = clusters[key]
            c["count"] += 1
            c["sessions"].add(sid)
            c["projects"].add(project)
    out = []
    for (kind, value), c in clusters.items():
        if c["count"] < min_count:
            continue
        out.append(
            {
                "kind": kind,
                "value": value,
                "count": c["count"],
                "sessions": sorted(c["sessions"])[:10],
                "projects": sorted(p for p in c["projects"] if p)[:5],
            }
        )
    if len(costs) >= 5:
        values = [c for c, _s, _p in costs]
        med = statistics.median(values)
        for cost, sid, project in costs:
            if med > 0 and cost > 4 * med and cost > 0.05:
                out.append(
                    {
                        "kind": "cost_outlier",
                        "value": f"${cost:.2f} vs median ${med:.2f}",
                        "count": 1,
                        "sessions": [sid],
                        "projects": [project],
                    }
                )
    out.sort(key=lambda c: (-c["count"], c["kind"], c["value"]))
    return out


TITLES = {
    "stop_reason": "runs stop with: {value}",
    "tool_skipped": "tool call skipped repeatedly: {value}",
    "guard": "guard keeps firing: {value}",
    "loop": "loop detected on {value}",
    "verifier_fail": "verifier ({value}) rejected changes",
    "cost_outlier": "cost outlier: {value}",
}


def file_cards(
    inbox: Inbox, clusters: list[dict[str, Any]], *, limit: int = 10
) -> list[dict[str, Any]]:
    cards = []
    for c in clusters[:limit]:
        title = TITLES.get(c["kind"], "{value}").format(value=c["value"])
        card = inbox.record(
            project=HARNESS_PROJECT,
            title=title,
            signature=f"{c['kind']}|{c['value']}",
            source="meta",
            evidence=[f"session {s}" for s in c["sessions"]],
            severity="high"
            if c["kind"] in {"stop_reason", "verifier_fail", "cost_outlier"}
            else "med",
            impacted_files=_suspects(c["kind"]),
        )
        cards.append(card)
    return cards


def _suspects(kind: str) -> list[str]:
    return {
        "stop_reason": ["trendlab/agent/runtime.py", "trendlab/agent/recovery.py"],
        "tool_skipped": ["trendlab/tools/runtime.py", "trendlab/permissions/engine.py"],
        "guard": ["trendlab/prompts/drivers/models", "trendlab/agent/evaluator.py"],
        "loop": ["trendlab/agent/loop_detector.py"],
        "verifier_fail": ["trendlab/agent/verifier.py"],
        "cost_outlier": ["trendlab/telemetry/costs.py", "trendlab/context/manager.py"],
    }.get(kind, [])


def counter_summary(clusters: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(c["kind"] for c in clusters))


def harness_root() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / ".git").exists():
            return parent
    return None
