"""IL-B improve job (evals spec §30.3-§30.5, D-115): a background draft the engine owns, crash
recovery, exact-path pruning and the socket-only trigger."""

import asyncio
import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_candidate_runner import NOW, Env, ok_tests
from trendlab.config.loader import trendlab_home
from trendlab.engine import candidate_runner as cr
from trendlab.engine import daemon as daemon_mod
from trendlab.engine import improve_policy, jobs
from trendlab.engine.daemon import Engine, client
from trendlab.engine.improve_policy import read_switch
from trendlab.engine.jobs import candidate_line, improve_job, prune_candidates, recover_drafting
from trendlab.sessions.store import SessionStore


@pytest.fixture(autouse=True)
def _fresh_lock():
    jobs._IMPROVE_LOCK = None  # created lazily inside each test's loop


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.decision()  # stamps last_seen at the test clock
    yield e
    e.inbox.close()


def gates_ok(env):
    async def gates(config):
        return {
            "isolation": {"ok": True, "reasons": []},
            "switch": read_switch(env.control),
            "sandbox_probe": {"ok": True, "reason": "ok"},
            "agent_sandbox": {"ok": True, "reason": "ok"},
            "consumed": frozenset(),
        }

    return gates


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


# -- the job -----------------------------------------------------------------------------------------
async def test_started_returns_at_once_and_sends_one_content_free_line(env):
    release = asyncio.Event()
    sent, spawned = [], []

    async def draft(inbox, config, decision):
        await release.wait()
        return {"candidate": "cand-x-1", "status": "handed_off", "spend_usd": 0.12,
                "tests": {"passed": 7, "failed": 0}}  # fmt: skip

    async def send(text):
        sent.append(text)
        return True

    def spawn(coro, cid):
        spawned.append((asyncio.create_task(coro), cid))

    res = await improve_job(env.inbox, env.cfg, {}, spawn=spawn, gates=gates_ok(env), draft=draft,
                            send=send, now=NOW)  # fmt: skip
    item = env.inbox.list(status="open")[0]
    assert res["started"] == f"cand-{item['id']}-1" and spawned[0][1] == res["started"]
    assert res["gates"]["sandbox_probe"]["ok"] and sent == []  # returned before the draft ended
    release.set()
    await spawned[0][0]
    assert sent == [
        "TrendLab IL-B · cand-x-1 · handed_off · spend $0.12 · tests 7 passed, 0 failed"
    ]


def test_the_telegram_line_carries_no_finding_content():
    line = candidate_line({"candidate": "cand-ab-2", "status": "tests_failed", "spend_usd": 0.3,
                           "tests": {"passed": 1, "failed": 2}, "detail": "tools case-1 secret"})  # fmt: skip
    assert (
        line == "TrendLab IL-B · cand-ab-2 · tests_failed · spend $0.30 · tests 1 passed, 2 failed"
    )
    assert "/home" not in line and "case-1" not in line
    assert candidate_line({"candidate": None, "status": "error", "spend_usd": None}) == (
        "TrendLab IL-B · no candidate · error · spend unknown"
    )


async def test_skips_with_reasons(env):
    env.switch(paused=True)
    res = await improve_job(env.inbox, env.cfg, {}, gates=gates_ok(env), now=NOW)
    assert res["skipped"].startswith("paused by the referee")
    env.cfg.improvement.propose = False
    assert (await improve_job(env.inbox, env.cfg, {}, now=NOW))["skipped"] == "propose is off"
    env.cfg.improvement.propose = True
    res = await improve_job(env.inbox, env.cfg, {}, busy=lambda: True, now=NOW)
    assert res["skipped"] == "a draft is already running"


async def test_item_is_plumbed_to_the_decision(env):
    res = await improve_job(env.inbox, env.cfg, {}, gates=gates_ok(env), item_id="nope", now=NOW)
    assert res["skipped"] == "nothing eligible: item nope is not eligible"


async def test_no_switch_file_means_skipped_paused(env):
    (env.control / "improve.json").unlink()
    res = await improve_job(env.inbox, env.cfg, {}, gates=gates_ok(env), now=NOW)
    assert res["skipped"].startswith("paused by the referee: no switch file")


# -- recovery ----------------------------------------------------------------------------------------
async def test_recovery_cleans_a_crashed_draft_and_trues_up_spend(env):
    # a crash between start and finish: the row and worktree exist, no finally ever ran
    cid = f"cand-{env.inbox.list(status='open')[0]['id']}-1"
    wt = env.dev / ".trendlab" / "worktrees" / cid
    env.inbox.start_candidate(
        item_id=cid.split("-")[1], case_prefix="evals|main|tools|case-1", day="2026-10-12",
        base_sha=env.base, reserved_usd=0.5, worktree=str(wt), branch=f"improve/{cid}",
    )  # fmt: skip
    env.inbox.set_candidate_session(cid, "sess-crash")
    wt.parent.mkdir(parents=True)
    sh("git", "worktree", "add", "-q", "-b", f"improve/{cid}", str(wt), env.base, cwd=env.dev)
    env.out.mkdir(parents=True)
    (env.out / f".{cid}.bundle.tmp").write_bytes(b"partial")
    (env.out / "keep.json").write_text("{}")
    store = SessionStore(trendlab_home() / "sessions.db")
    store.record_model_call("sess-crash", "deepseek:flash", "default", 1, 1, 0, 1, 0.07)
    store.close()
    assert await recover_drafting(env.inbox, env.cfg, is_live=lambda c: c == cid) == []
    assert await recover_drafting(env.inbox, env.cfg) == [cid]
    row = env.inbox.candidate(cid)
    assert row["status"] == "error" and row["detail"] == "recovered after crash or restart"
    assert row["spend_usd"] == pytest.approx(0.07)
    assert not wt.exists() and cid not in sh("git", "worktree", "list", cwd=env.dev)
    assert sh("git", "branch", "--list", "improve/*", cwd=env.dev) == ""
    assert sorted(p.name for p in env.out.iterdir()) == ["keep.json"]


async def test_recovery_without_records_keeps_the_reservation(env):
    env.inbox.start_candidate(
        item_id="x1", case_prefix="evals|main|tools|case-1", day="2026-10-12", base_sha=env.base,
        reserved_usd=0.4, worktree="/not/a/worktree", branch="improve/cand-x1-1",
    )  # fmt: skip
    assert await recover_drafting(env.inbox, env.cfg) == ["cand-x1-1"]
    assert env.inbox.spent_today("2026-10-12") == pytest.approx(0.4)


# -- pruning -----------------------------------------------------------------------------------------
async def test_prune_deletes_exact_files_only(env):
    env.out.mkdir(parents=True)
    old = datetime.now(UTC) - timedelta(days=15)
    ids = []
    for n, status in enumerate(("handed_off", "handed_off", "tests_failed", "handed_off"), 1):
        c = env.inbox.start_candidate(
            item_id=f"it{n}", case_prefix=f"evals|main|c|k{n}", day="2026-10-01",
            base_sha=env.base, reserved_usd=0.1,
            worktree=str(env.dev / ".trendlab" / "worktrees" / f"cand-it{n}-1"),
            branch=f"improve/cand-it{n}-1",
        )  # fmt: skip
        env.inbox.finish_candidate(c["id"], status=status, spend_usd=0.1)
        ids.append(c["id"])
    with env.inbox._lock:
        for cid in ids[:3]:
            env.inbox._conn.execute(
                "UPDATE candidate SET started_at=? WHERE id=?", (old.isoformat(), cid)
            )
        env.inbox._conn.commit()
    for cid in ids:
        (env.out / f"{cid}.bundle").write_bytes(b"b")
        (env.out / f"{cid}.json").write_text("{}")
    (env.out / "unrelated.txt").write_text("x")
    (env.out / "cand-it1-1.bundle.bak").write_text("x")
    kept_wt = env.dev / ".trendlab" / "worktrees" / ids[2]
    kept_wt.parent.mkdir(parents=True)
    sh("git", "worktree", "add", "-q", "-b", f"improve/{ids[2]}", str(kept_wt), env.base,
       cwd=env.dev)  # fmt: skip
    pruned = await prune_candidates(env.inbox, env.cfg, consumed=frozenset({ids[1]}))
    assert pruned == [ids[0], ids[2]]
    left = sorted(p.name for p in env.out.iterdir())
    assert left == sorted([
        "cand-it1-1.bundle.bak", "unrelated.txt", f"{ids[1]}.bundle", f"{ids[1]}.json",
        f"{ids[2]}.bundle", f"{ids[2]}.json", f"{ids[3]}.bundle", f"{ids[3]}.json",
    ])  # fmt: skip
    assert not kept_wt.exists()
    assert env.inbox.candidate(ids[0])["status"] == "pruned"
    assert env.inbox.candidate(ids[1])["status"] == "handed_off"  # consumed by the referee
    assert env.inbox.candidate(ids[3])["status"] == "handed_off"  # too recent
    assert env.inbox.pending_handoffs() == 2


# -- the engine --------------------------------------------------------------------------------------
def engine_for(env, monkeypatch):
    eng = Engine(projects=[env.tmp], config=env.cfg, tick_seconds=0.05)
    eng.inbox.close()
    eng.inbox = env.inbox
    monkeypatch.setattr(jobs, "improve_gates", gates_ok(env))
    monkeypatch.setattr(improve_policy, "ny_now", lambda: NOW)
    return eng


def test_improve_due_rules(env, monkeypatch):
    eng = engine_for(env, monkeypatch)
    now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    eng.started -= 1000
    assert not eng.due("improve", now)  # scheduled is off: manual runs only
    env.cfg.improvement.scheduled = True
    assert eng.due("improve", now)
    env.cfg.improvement.propose = False
    assert not eng.due("improve", now)
    env.cfg.improvement.propose = True
    eng.state["last_improve"] = time.time() - 3600
    assert not eng.due("improve", now)  # every 2 hours at most
    eng.state["last_improve"] -= 3600
    assert eng.due("improve", now)
    eng.started = time.time()
    assert not eng.due("improve", now)  # warm-up
    eng.started -= 1000

    async def busy():
        eng._spawn_draft(asyncio.sleep(5), "cand-x-1")
        assert not eng.due("improve", now)  # a draft is running
        eng.draft_task.cancel()

    asyncio.run(busy())


async def test_engine_runs_other_jobs_during_a_draft_and_two_triggers_give_one(env, monkeypatch):
    eng = engine_for(env, monkeypatch)
    release = asyncio.Event()
    starts = []

    async def fake_draft(inbox, config, decision, **kw):
        starts.append(decision.item["id"])
        await release.wait()
        return {"candidate": "c", "status": "no_change", "spend_usd": 0.0}

    monkeypatch.setattr(cr, "draft_candidate", fake_draft)

    async def no_send(config, text):
        return True

    monkeypatch.setattr("trendlab.engine.notify.send_telegram", no_send)
    a, b = await asyncio.gather(eng.run_job("improve"), eng.run_job("improve"))
    results = sorted([a["improve"], b["improve"]], key=lambda r: "started" in r)
    assert results[0]["skipped"] == "a draft is already running" and "started" in results[1]
    await asyncio.sleep(0)
    assert eng.draft_live() and eng.status()["draft"]["candidate"] == results[1]["started"]
    digest = await eng.run_job("digest")  # other jobs keep running
    assert "error" not in digest
    release.set()
    await eng.draft_task
    await asyncio.sleep(0)
    assert not eng.draft_live() and eng.status()["draft"] is None and len(starts) == 1


async def test_sigterm_cancels_the_draft_and_it_records_shutdown(env, monkeypatch):
    eng = engine_for(env, monkeypatch)
    gate = asyncio.Event()

    from trendlab.telemetry.costs import CostTracker

    async def run_agent(path, cfg, prompt, hooks):
        hooks.started("sess-term", CostTracker(cfg))
        gate.set()
        await asyncio.sleep(30)

    real = cr.draft_candidate

    async def draft(inbox, config, decision, **kw):
        return await real(inbox, config, decision, run_agent=run_agent, run_tests=ok_tests())

    monkeypatch.setattr(cr, "draft_candidate", draft)
    task = asyncio.create_task(eng.run())
    for _ in range(100):
        await asyncio.sleep(0.02)
        if daemon_mod.socket_path().exists():
            break
    resp = await client("run", job="improve")
    assert "started" in resp["improve"], resp
    await asyncio.wait_for(gate.wait(), 10)
    eng._stop.set()  # what the SIGTERM handler does
    await asyncio.wait_for(task, timeout=30)  # the engine closed the inbox after the draft
    from trendlab.engine.inbox import Inbox

    env.inbox = Inbox(env.tmp / "inbox.db")
    (row,) = env.inbox.candidates()
    assert row["status"] == "shutdown"
    assert "improve/" not in sh("git", "worktree", "list", cwd=env.dev)


async def test_socket_forwards_item(env, monkeypatch):
    eng = engine_for(env, monkeypatch)
    seen = {}

    async def fake_job(inbox, config, state, **kw):
        seen.update(kw)
        return {"skipped": "test"}

    monkeypatch.setattr(jobs, "improve_job", fake_job)
    task = asyncio.create_task(eng.run())
    for _ in range(100):
        await asyncio.sleep(0.02)
        if daemon_mod.socket_path().exists():
            break
    resp = await client("run", job="improve", item="abc123")
    assert resp["improve"] == {"skipped": "test"} and seen["item_id"] == "abc123"
    await client("stop")
    await asyncio.wait_for(task, timeout=5)


async def test_observe_tick_recovers_and_prunes_daily(env, monkeypatch):
    eng = engine_for(env, monkeypatch)
    env.cfg.improvement.observe = False
    env.inbox.start_candidate(
        item_id="x2", case_prefix="evals|main|tools|case-1", day="2026-10-12", base_sha=env.base,
        reserved_usd=0.1, worktree="/nowhere", branch="improve/cand-x2-1",
    )  # fmt: skip
    res = await eng.run_job("observe")
    assert res["recovered"] == ["cand-x2-1"] and res["improve_pruned"] == []
    again = await eng.run_job("observe")
    assert "improve_pruned" not in again  # once a day


def test_engine_run_improve_is_socket_only(monkeypatch):
    from typer.testing import CliRunner

    from trendlab.cli import app

    async def never(self, job, **kw):
        raise AssertionError("improve must never run in-process")

    monkeypatch.setattr(Engine, "run_job", never)
    daemon_mod.pid_path().write_text(str(os.getpid()))
    r = CliRunner().invoke(app, ["engine", "run", "improve", "--item", "abc"])
    assert r.exit_code == 1 and "improve runs inside the engine" in r.output
    r = CliRunner().invoke(app, ["improve", "prune"])
    assert r.exit_code == 1 and "improve prune runs inside the engine" in r.output


def test_cli_forwards_item_to_the_socket(monkeypatch):
    from typer.testing import CliRunner

    from trendlab.cli import app

    calls = []

    async def fake_client(cmd, **kw):
        calls.append((cmd, kw))
        return {"improve": {"started": "cand-abc-1"}}

    monkeypatch.setattr(daemon_mod, "client", fake_client)
    r = CliRunner().invoke(app, ["engine", "run", "improve", "--item", "abc"])
    assert r.exit_code == 0 and calls == [("run", {"job": "improve", "item": "abc"})]
    assert json.loads(r.output)["improve"]["started"] == "cand-abc-1"
