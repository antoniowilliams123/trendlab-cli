"""Engine jobs that need a model: sleeptime consolidation and the meta-loop scan."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trendlab.config.loader import trendlab_home
from trendlab.engine.inbox import Inbox
from trendlab.engine.meta import counter_summary, file_cards, scan
from trendlab.engine.sleep import sleeptime
from trendlab.providers.gateway import ModelGateway
from trendlab.providers.registry import resolve_role
from trendlab.sessions.store import SessionStore
from trendlab.telemetry.events import EventBus


def _caller(config, role: str):
    ref = resolve_role(config, role, config.defaults.model)
    gateway = ModelGateway(config, EventBus(), "engine")

    async def call(messages):
        response, _used = await gateway.complete(ref, messages, None)
        return response.text

    return call


async def sleep_projects(projects: list[Path], config) -> list[dict[str, Any]]:
    store = SessionStore(trendlab_home() / "sessions.db")
    try:
        out = []
        for root in projects:
            if not root.is_dir():
                continue
            res = await sleeptime(
                _caller(config, "summarizer"),
                store=store,
                project_root=root,
                model_ref=config.defaults.model,
            )
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
