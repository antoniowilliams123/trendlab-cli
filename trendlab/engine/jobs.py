"""Engine jobs that need a model: sleeptime consolidation and the meta-loop scan."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trendlab.benchmarks.runner import CANARY, append_bench_log, run_suite, summarize
from trendlab.config.loader import trendlab_home
from trendlab.engine.inbox import Inbox
from trendlab.engine.meta import counter_summary, file_cards, scan
from trendlab.engine.sleep import sleeptime
from trendlab.sessions.store import SessionStore


def _caller(config, role: str):
    """Engine model calls are recorded under an '(engine)' session (fully loaded cost, U10)."""
    from trendlab.telemetry.recorded import RecordedCaller

    return RecordedCaller(config, role, "(engine)")


async def sleep_projects(projects: list[Path], config) -> list[dict[str, Any]]:
    store = SessionStore(trendlab_home() / "sessions.db")
    try:
        out = []
        for root in projects:
            if not root.is_dir():
                continue
            call = _caller(config, "summarizer")
            try:
                res = await sleeptime(
                    call, store=store, project_root=root, model_ref=config.defaults.model
                )
            finally:
                await call.close()
            out.append({"project": str(root), **res})
        return out
    finally:
        store.close()


async def meta_draft(inbox: Inbox, config, *, issue_id: str | None = None) -> dict[str, Any]:
    """Draft a fix for the top harness card in a worktree of trendlab-cli (§7.4). The run uses
    the worktree workspace, so only a verified diff is applied; otherwise the patch is parked."""
    from rich.console import Console

    from trendlab.app import TrendLabApp
    from trendlab.config.schema import PermissionMode
    from trendlab.engine.meta import HARNESS_PROJECT, harness_root

    root = harness_root()
    if root is None:
        return {"ok": False, "reason": "not running from a trendlab-cli checkout"}
    item = (
        inbox.get(issue_id) if issue_id else next(iter(inbox.list(HARNESS_PROJECT, limit=1)), None)
    )
    if item is None:
        return {"ok": False, "reason": "no open harness cards"}
    config.verification.workspace = "worktree"
    config.defaults.permission_mode = PermissionMode.UNSAFE
    tl = TrendLabApp(root, config, console=Console(quiet=True))
    from trendlab.benchmarks.unattended import Unattended

    Unattended(tl)  # the meta-loop runs unattended
    await tl.start(interactive=False)
    try:
        prompt = (
            "You are improving the TrendLab CLI harness itself. Meta-loop finding: "
            f"{item['title']}. Evidence: {', '.join(item['evidence_refs'][:5])}. "
            f"Suspected files: {', '.join(item['impacted_files']) or 'unknown'}. "
            "Find the cause in the harness code, make the smallest fix, add a test under tests/, "
            "and run the test suite."
        )
        result = await tl.run_prompt(prompt)
    finally:
        await tl.stop()
    verdict = (result.verification or {}).get("verdict")
    inbox.attach(item["id"], verification_ref=str(verdict), feedback=f"draft: {result.status}")
    if result.status == "COMPLETED" and verdict != "fail":
        inbox.set_status(item["id"], "applied")
    return {
        "ok": True,
        "issue": item["id"],
        "status": result.status,
        "verdict": verdict,
        "changed": result.changed_files,
        "cost": round(result.cost_usd, 4),
    }


async def canary_run(inbox: Inbox, config, state: dict[str, Any]) -> dict[str, Any]:
    """Nightly canary (U1/U3): the fixed 10-task set on the default model, compared with the
    previous canary. A drop of ``canary_drop_alert`` tasks or more files a drift card whose
    root cause names what changed: the served model id, the system prompt, or neither."""
    import json as _json
    from datetime import datetime

    from trendlab.benchmarks.stats import paired_outcomes

    model = config.defaults.model
    rows = await run_suite(model, profile="harness", selector=CANARY, tier="all")
    summary = summarize(rows)
    served = sorted({(r.get("model_served") or [""])[0] for r in rows if not r.get("skipped")})
    phash = sorted({(r.get("prompt_hash") or [""])[0] for r in rows if not r.get("skipped")})
    prev_rows = state.get("canary_rows") or []
    prev_meta = state.get("canary_meta") or {}
    report: dict[str, Any] = {
        "model": model,
        "model_served": served,
        "prompt_hash": phash,
        "passes": summary["passes"],
        "cost": summary["cost"],
    }
    if prev_rows:
        paired = paired_outcomes(prev_rows, rows, "passes")
        report["vs_previous"] = {k: paired[k] for k in ("b_wins", "b_losses", "p_value")}
        hints = []
        if prev_meta.get("model_served") and prev_meta["model_served"] != served:
            hints.append(f"model version changed {prev_meta['model_served']} → {served}")
        if prev_meta.get("prompt_hash") and prev_meta["prompt_hash"] != phash:
            hints.append("system prompt changed")
        prev_wall = (state.get("canary_summary") or {}).get("wall_s")
        if prev_wall and summary.get("wall_s") and summary["wall_s"] > 1.5 * prev_wall:
            hints.append(
                f"performance regression: wall time {summary['wall_s']:.0f}s vs {prev_wall:.0f}s"
            )
            report["performance_regression"] = True
        prev_cost = (state.get("canary_summary") or {}).get("cost")
        if prev_cost and summary.get("cost") and summary["cost"] > 1.5 * prev_cost:
            hints.append(f"cost regression: ${summary['cost']:.3f} vs ${prev_cost:.3f}")
            report["cost_regression"] = True
        if not hints:
            hints.append("same model id and prompt: provider behaviour or flakiness")
        report["cause_hints"] = hints
        if paired["b_losses"] >= config.engine.canary_drop_alert:
            card = inbox.record(
                project="harness:canary",
                title=f"canary drop: {model} lost {paired['b_losses']} task(s) vs previous night",
                signature=f"canary_drop|{model}",
                source="canary",
                root_cause="; ".join(hints)
                + " · lost: "
                + ", ".join(paired["tasks_only_a_passes"]),
                evidence=[f"p={paired['p_value']}", f"passes {summary['passes']}"],
                severity="high",
            )
            report["card"] = card["id"]
    state["canary_rows"] = [
        {"task": r["task"], "passes": r.get("passes"), "skipped": r.get("skipped")} for r in rows
    ]
    state["canary_summary"] = summary
    state["canary_meta"] = {"model_served": served, "prompt_hash": phash}
    hist = trendlab_home() / "engine" / "canary_history.jsonl"
    try:
        hist.parent.mkdir(parents=True, exist_ok=True)
        with hist.open("a", encoding="utf-8") as fh:
            fh.write(
                _json.dumps(
                    {
                        "at": datetime.now().isoformat(timespec="seconds"),
                        **report,
                        "passes_ci95": summary.get("passes_ci95"),
                        "cost_per_task_ci95": summary.get("cost_per_task_ci95"),
                    }
                )
                + "\n"
            )
    except OSError:
        pass
    try:
        append_bench_log(trendlab_home() / "engine" / "CANARY_LOG.md", f"canary {model}", rows)
    except Exception:  # noqa: BLE001
        pass
    return report


async def budget_check(inbox: Inbox, config, state: dict[str, Any], *, now=None) -> dict[str, Any]:
    """U10: month-to-date spend vs [economics] budgets; each crossed threshold alerts once."""
    from datetime import datetime

    from trendlab.engine.meta import HARNESS_PROJECT
    from trendlab.engine.notify import send_telegram
    from trendlab.telemetry.ledger import budget_status, new_alerts

    store = SessionStore(trendlab_home() / "sessions.db")
    try:
        status = budget_status(store, config.economics, now=now)
    finally:
        store.close()
    month = (now or datetime.now()).strftime("%Y-%m")
    alerts = new_alerts(status, state, month)
    for alert in alerts:
        inbox.record(
            project=HARNESS_PROJECT,
            title=alert["text"],
            # words, not numbers: the inbox clusters signatures with digits normalised away
            signature=f"budget|{alert['budget']}|"
            + ("over budget" if alert["threshold"] >= 1.0 else "near budget"),
            source="budget",
            evidence=[f"{b['budget']}: ${b['spent']:.2f} of ${b['limit']:.2f}" for b in status],
            severity="high" if alert["threshold"] >= 1.0 else "med",
        )
        try:
            await send_telegram(config, alert["text"])
        except Exception:  # noqa: BLE001 — an alert channel failure must not stop the job
            pass
    return {"budgets": status, "alerts": [a["text"] for a in alerts]}


async def meta_scan(inbox: Inbox, config, *, days: int = 7) -> dict[str, Any]:
    store = SessionStore(trendlab_home() / "sessions.db")
    try:
        clusters = scan(store, days=days)
    finally:
        store.close()
    cards = file_cards(inbox, clusters)
    return {
        "clusters": len(clusters),
        "by_kind": counter_summary(clusters),
        "cards": [c["id"] for c in cards],
    }
