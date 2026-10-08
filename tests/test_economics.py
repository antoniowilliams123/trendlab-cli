"""Uplift U10: cost ledger, ROI, waste, budgets and alerts, fully loaded cost persistence."""

import json
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from typer.testing import CliRunner

from trendlab.app import TrendLabApp
from trendlab.benchmarks.runner import _roi_delta, roi
from trendlab.cli import app
from trendlab.config.loader import load_config, trendlab_home
from trendlab.config.schema import EconomicsConfig, PermissionMode, RemoteApprovalConfig
from trendlab.engine.inbox import Inbox
from trendlab.engine.jobs import budget_check
from trendlab.providers.base import ModelResponse
from trendlab.providers.scripted import ScriptedProvider
from trendlab.sessions.store import SessionStore
from trendlab.telemetry.ledger import bucket, budget_status, ledger, new_alerts


def _sid(store, path, model="deepseek:deepseek-flash"):
    s = store.create_session(path, model)
    return s["id"] if isinstance(s, dict) else store.latest_session(path)["id"]


def _now():
    return datetime.now(UTC).isoformat()


def test_bucket_keeps_benchmark_and_scratch_spend_apart():
    assert bucket("/tmp/trendlab-suite-py01-abc/repo") == "(benchmarks)"
    assert bucket("/tmp/tl-noisy-x/repo") == "(benchmarks)"
    assert bucket("/tmp/whatever") == "(scratch)"
    assert bucket(str(Path.home())) == "~ (home)"
    assert bucket("/home/me/projects/app") == "/home/me/projects/app"
    assert bucket(None) == "(unknown)"


def _seed(store):
    work = _sid(store, "/home/me/projects/app")
    store.record_model_call(work, "deepseek:deepseek-flash", "main", 1000, 100, 800, 5, 0.006)
    store.record_model_call(work, "deepseek:deepseek-v4-pro", "planner", 500, 50, 0, 5, 0.004)
    store.append_event(
        work, "run.completed", {"changed_files": ["a.py"], "validated": True}, _now()
    )
    store.append_event(work, "run.completed", {"changed_files": [], "validated": False}, _now())
    dead = _sid(store, "/home/me/projects/app")
    store.record_model_call(dead, "deepseek:deepseek-flash", "main", 900, 90, 0, 5, 0.005)
    store.append_event(dead, "run.failed", {"stop_reason": "cost limit $0.01 reached"}, _now())
    bench = _sid(store, "/tmp/trendlab-suite-py01-x/repo")
    store.record_model_call(bench, "deepseek:deepseek-flash", "main", 100, 10, 0, 5, 0.002)
    store.append_event(bench, "run.completed", {"changed_files": ["x"], "validated": True}, _now())


def test_ledger_roi_overhead_waste_and_cache(tmp_path: Path):
    store = SessionStore(tmp_path / "s.db")
    _seed(store)
    led = ledger(store, 30)
    store.close()
    assert led["total"] == 0.017 and led["previous_period"] == 0.0 and led["change"] is None
    app_row = next(r for r in led["projects"] if r["project"] == "/home/me/projects/app")
    assert app_row["cost"] == 0.015 and app_row["runs"] == 3 and app_row["completed"] == 2
    assert app_row["validated_changes"] == 1 and app_row["cost_per_validated_change"] == 0.015
    assert app_row["overhead_share"] == 0.267  # planner spend counts: fully loaded cost
    assert app_row["wasted"] == 0.005  # the session whose only run failed
    assert app_row["cache_share"] == round(800 / 2400, 3)
    assert app_row["by_role"] == {"main": 0.011, "planner": 0.004}
    assert app_row["p95_input_tokens"] == 900 and app_row["heavy_call_share"] == 0.0
    assert led["work_share"] == round(0.015 / 0.017, 3)
    assert any(r["project"] == "(benchmarks)" for r in led["projects"])


def test_budgets_alert_once_per_threshold_per_month(tmp_path: Path):
    store = SessionStore(tmp_path / "s.db")
    _seed(store)
    econ = EconomicsConfig(monthly_budget_usd=0.02, project_budgets={"/home/me/projects/app": 0.01})
    status = budget_status(store, econ)
    store.close()
    by = {b["budget"]: b for b in status}
    assert by["all projects"]["crossed"] == [0.8] and by["all projects"]["share"] == 0.85
    assert by["/home/me/projects/app"]["crossed"] == [0.8, 1.0]
    state: dict = {}
    first = new_alerts(status, state, "2026-10")
    assert len(first) == 3 and {a["threshold"] for a in first} == {0.8, 1.0}
    assert new_alerts(status, state, "2026-10") == []  # already alerted this month
    assert len(new_alerts(status, state, "2026-11")) == 3  # a new month re-arms


async def test_budget_job_files_cards_and_sends_once(
    _trendlab_home: Path, tmp_path: Path, monkeypatch
):
    store = SessionStore(trendlab_home() / "sessions.db")
    _seed(store)
    store.close()
    sent = []

    async def fake_send(config, text):
        sent.append(text)
        return True

    monkeypatch.setattr("trendlab.engine.notify.send_telegram", fake_send)
    cfg = load_config()
    cfg.economics.monthly_budget_usd = 0.01
    inbox = Inbox(tmp_path / "inbox.db")
    state: dict = {}
    res = await budget_check(inbox, cfg, state)
    again = await budget_check(inbox, cfg, state)
    cards = inbox.list(None, status=None)
    inbox.close()
    assert len(res["alerts"]) == 2 and again["alerts"] == [] and len(sent) == 2
    assert any(c["severity"] == "high" for c in cards)  # 100% crossed


def test_roi_and_what_extra_spend_bought():
    a = [
        {"task": "x", "passes": True, "regression_added": False, "cost": 0.004},
        {"task": "y", "passes": True, "regression_added": True, "cost": 0.004},
    ]
    b = [
        {"task": "x", "passes": True, "regression_added": True, "cost": 0.005},
        {"task": "y", "passes": True, "regression_added": True, "cost": 0.005},
    ]
    assert roi(a)["cost_per_pass"] == 0.004 and roi(a)["passes_per_usd"] == 250.0
    d = _roi_delta(a, b)
    assert d["extra_cost"] == 0.002 and d["usd_per_extra_pass"] is None  # no extra passes
    assert d["usd_per_extra_tested_pass"] == 0.002  # but one more pass with a regression test


async def test_every_role_is_persisted(project: Path, _trendlab_home: Path):
    cfg = load_config(project)
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    plan = '{"steps": [{"title": "Fix it", "files": ["src/app.py"], "done_when": "works"}]}'
    tl = TrendLabApp(
        project,
        cfg,
        provider=ScriptedProvider([ModelResponse(text=plan)]),
        model_ref="scripted:m",
        permission_mode=PermissionMode.UNSAFE,
        console=Console(quiet=True),
    )
    await tl.start(interactive=False)
    try:
        await tl._plan_steps("fix the timeout")  # an auxiliary (planner) call, not the lead
        sid = tl.session_id
    finally:
        await tl.stop()
    store = SessionStore(trendlab_home() / "sessions.db")
    rows = store.spend_rows("2000-01-01")
    store.close()
    assert [r["role"] for r in rows if r["session_id"] == sid] == ["planner"]


def test_cost_command_and_stats_include_communication(_trendlab_home: Path):
    store = SessionStore(trendlab_home() / "sessions.db")
    _seed(store)
    sid = _sid(store, "/home/me/projects/app")
    store.append_event(
        sid,
        "communication.checked",
        {"words": 40, "reading_ease": 70.0, "robospeak": [], "issues": []},
        _now(),
    )
    st = store.stats(7)
    store.close()
    assert st["communication"]["answers"] == 1  # regression: the stats query dropped these
    out = CliRunner().invoke(app, ["cost", "--output", "json"])
    data = json.loads(out.output)
    assert data["total"] == 0.017 and data["budgets"] == []
    text = CliRunner().invoke(app, ["cost", "--project", "/home/me/projects/app"])
    assert "/home/me/projects/app" in text.output and "(benchmarks)" not in text.output
