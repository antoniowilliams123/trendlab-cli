"""Engine (cheap-model spec §7): inbox clustering and actions, digest, drafter, sleeptime,
meta-loop scan, daemon scheduling and socket, watch, failed runs filed automatically."""

import asyncio
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console

from trendlab.config.loader import load_config, trendlab_home
from trendlab.config.schema import AppConfig, PermissionMode, RemoteApprovalConfig
from trendlab.engine import daemon as daemon_mod
from trendlab.engine.daemon import Engine, client
from trendlab.engine.drafter import parse_card, render_card
from trendlab.engine.inbox import Inbox, cluster_key, digest
from trendlab.engine.meta import HARNESS_PROJECT, file_cards, scan
from trendlab.engine.sleep import day_digest, parse_plan, sleeptime
from trendlab.engine.watch import watch_project
from trendlab.orchestration.gitflow import narrow_exclude
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.sessions.store import SessionStore


def _ts():
    return datetime.now(UTC).isoformat()


def test_inbox_clusters_and_actions(tmp_path: Path):
    inbox = Inbox(tmp_path / "inbox.db")
    a = inbox.record(
        project="/p",
        title="test failing: tests/test_x.py::test_a",
        signature="FAILED tests/test_x.py::test_a - assert 1 == 2",
        evidence=["s1"],
    )
    b = inbox.record(
        project="/p",
        title="test failing: tests/test_x.py::test_a",
        signature="FAILED tests/test_x.py::test_a - assert 3 == 4",
        evidence=["s2"],
    )
    assert a["id"] == b["id"] and b["occurrences"] == 2 and b["evidence_refs"] == ["s1", "s2"]
    assert cluster_key("Error at 0xdeadbeef line 12") == cluster_key("Error at 0xcafebabe line 99")
    c = inbox.record(project="/p", title="other", signature="KeyError: 'qty'", severity="high")
    assert [i["id"] for i in inbox.list("/p")] == [c["id"], a["id"]]  # high severity first
    assert inbox.set_status(a["id"], "dismissed", feedback="not a bug")
    assert inbox.counts("/p") == {"open": 1, "dismissed": 1}
    # a dismissed cluster that comes back is reopened
    again = inbox.record(
        project="/p", title="x", signature="FAILED tests/test_x.py::test_a - assert 5 == 6"
    )
    assert again["status"] == "open" and again["occurrences"] == 3
    assert inbox.find(a["id"][:4], "/p")["id"] == a["id"] and inbox.find("zzz") is None
    inbox.attach(a["id"], card={"problem": "p"}, verification_ref="pass")
    assert inbox.get(a["id"])["card"] == {"problem": "p"}
    text = digest(inbox.list("/p"), inbox.counts("/p"), "/p")
    assert (
        text.startswith("Inbox · p: 2 open") and "[high] other" in text and f"id {c['id']}" in text
    )
    inbox.close()


def test_drafter_card_parsing():
    assert parse_card("nope") is None
    card = parse_card(
        '{"problem": "crash on empty list", "root_cause": "no guard", "impacted_files": ["a.py", 3], "evidence_refs": [], "proposed_change": "add guard", "test_results": "unknown"}'
    )
    assert card["impacted_files"] == ["a.py", "3"] and "Problem: crash" in render_card(card)


def _session(store, project: Path, model="scripted:m"):
    sess = store.create_session(str(project), model)
    return sess["id"] if isinstance(sess, dict) else store.latest_session(str(project))["id"]


async def test_sleeptime_consolidates_memory_and_opens_branch(project: Path, _trendlab_home: Path):
    subprocess.run(["git", "-C", str(project), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-qm",
            "init",
        ],
        check=True,
    )
    (project / ".trendlab").mkdir()
    (project / ".trendlab/memory.md").write_text(
        "# memory\n- [2026-10-01] Tests run with pytest\n- [2026-10-01] old fact\n"
    )
    store = SessionStore(trendlab_home() / "sessions.db")
    sid = _session(store, project)
    store.append_message(sid, {"role": "user", "content": "please add src/new.py"})
    store.append_event(sid, "run.steered", {"text": "use ruff not black"}, _ts()) if hasattr(
        store, "append_event"
    ) else None
    text = day_digest(store, str(project.resolve()))
    assert "please add src/new.py" in text
    answers = [
        json.dumps(
            {
                "facts": ["Tests run with pytest", "Lint with ruff, never black"],
                "instructions": "Run ruff before committing.",
                "model_notes": "Prefer patch_file over write_file here.",
                "skills": [{"name": "Release Steps", "instructions": "1. bump version"}],
            }
        )
    ]

    async def call(messages):
        assert "old fact" in messages[0]["content"]
        return answers.pop(0)

    res = await sleeptime(
        call, store=store, project_root=project, model_ref="deepseek:deepseek-flash"
    )
    store.close()
    assert (
        res["ok"]
        and res["facts"] == 2
        and res["branch"] == f"trendlab/memory-{datetime.now(UTC).strftime('%Y-%m-%d')}"
    )
    mem = (project / ".trendlab/memory.md").read_text()
    assert (
        "old fact" not in mem
        and "[2026-10-01] Tests run with pytest" in mem
        and "Lint with ruff" in mem
    )
    assert "Run ruff before committing." in (project / "TRENDLAB.md").read_text()
    assert (
        (project / ".trendlab/skills/_model/deepseek-deepseek-flash.md")
        .read_text()
        .strip()
        .endswith("Prefer patch_file over write_file here.")
    )
    assert (project / ".trendlab/skills/release-steps/SKILL.md").is_file()
    branches = subprocess.run(
        ["git", "-C", str(project), "branch", "--list"], capture_output=True, text=True
    ).stdout
    assert "trendlab/memory-" in branches
    shown = subprocess.run(
        ["git", "-C", str(project), "show", "--stat", "--format=%s", res["branch"]],
        capture_output=True,
        text=True,
    ).stdout
    assert "sleeptime memory consolidation" in shown and ".trendlab/memory.md" in shown
    assert "!.trendlab/memory.md" in (project / ".git/info/exclude").read_text()
    # the working branch is untouched
    assert "main" in branches or "master" in branches
    assert parse_plan("x") is None


def test_meta_scan_clusters_signals(project: Path, _trendlab_home: Path, tmp_path: Path):
    store = SessionStore(trendlab_home() / "sessions.db")
    for n in range(3):
        sid = _session(store, project)
        store.append_event(sid, "run.failed", {"stop_reason": "max_iterations (50) reached"}, _ts())
        store.append_event(
            sid,
            "guard.fired",
            {"guard": "announced_action", "model": "deepseek:deepseek-flash"},
            _ts(),
        )
        if n == 0:
            store.append_event(sid, "verify.verdict", {"verdict": "fail", "model": "strong"}, _ts())
    clusters = scan(store, days=7, min_count=2)
    store.close()
    kinds = {(c["kind"], c["value"]): c["count"] for c in clusters}
    assert kinds[("failure", "BUDGET_ITERATIONS")] == 3  # clustered by taxonomy code
    assert kinds[("guard", "announced_action@deepseek:deepseek-flash")] == 3
    assert ("verifier_fail", "strong") not in kinds  # below min_count
    inbox = Inbox(tmp_path / "inbox.db")
    cards = file_cards(inbox, clusters)
    assert cards and cards[0]["project"] == HARNESS_PROJECT and cards[0]["source"] == "meta"
    assert cards[0]["evidence_refs"] and cards[0]["impacted_files"]
    inbox.close()


async def test_watch_files_failing_tests(project: Path, tmp_path: Path):
    (project / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts='-q -p no:cacheprovider'\n"
    )
    (project / "test_w.py").write_text(
        "def test_ok():\n    assert True\n\ndef test_bad():\n    assert 1 == 2\n"
    )
    inbox = Inbox(tmp_path / "inbox.db")
    cfg = AppConfig()
    issues = await watch_project(inbox, project, cfg)
    assert len(issues) == 1 and issues[0]["title"] == "test failing: test_w.py::test_bad"
    assert issues[0]["impacted_files"] == ["test_w.py"] and issues[0]["severity"] == "high"
    (project / "test_w.py").write_text("def test_ok():\n    assert True\n")
    assert await watch_project(inbox, project, cfg) == []
    inbox.close()


async def test_engine_schedule_socket_and_digest(tmp_path: Path, monkeypatch, _trendlab_home: Path):
    cfg = AppConfig()
    cfg.engine.watch_minutes = 5
    cfg.engine.sleep_at = "02:30"
    eng = Engine(projects=[tmp_path], config=cfg, tick_seconds=0.05)
    now = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)
    assert not eng.due("sleep", now) and not eng.due("meta", now)  # warm-up: no model jobs yet
    eng.started -= 1000
    assert eng.due("watch", now) and eng.due("meta", now) and eng.due("digest", now)
    assert eng.due("sleep", now)  # 03:00 >= 02:30 and not done today
    eng.state["sleep_day"] = "2026-10-08"
    assert not eng.due("sleep", now)
    eng.state.pop("sleep_day")
    eng.state["last_sleep"] = now.timestamp() - 3600  # ran an hour ago → not again
    assert not eng.due("sleep", now)
    eng.state.pop("last_sleep")
    # the job stamps the day in the same (local) zone the schedule reads
    await eng.run_job("sleep")  # no model key in tests: the job errors but still stamps the day
    assert eng.state["sleep_day"] == eng.now().strftime("%Y-%m-%d")
    assert not eng.due("sleep", eng.now())
    eng.state["last_watch"] = now.timestamp()
    assert not eng.due("watch", now)
    sent = []

    async def fake_send(config, text):
        sent.append(text)
        return True

    monkeypatch.setattr(daemon_mod, "send_telegram", fake_send)
    assert await eng.send_digest() is False  # nothing open → no message
    eng.inbox.record(project=str(tmp_path), title="broken thing", signature="x", severity="high")
    assert await eng.send_digest() is True and "broken thing" in sent[0]
    assert await eng.send_digest() is False  # already-reported issues are not re-sent
    eng.inbox.record(project=str(tmp_path), title="test failing: tests/test_y.py::t", signature="y")
    assert await eng.send_digest() is True and len(sent) == 2
    assert "A test is failing:" in sent[1] and "broken thing" not in sent[1]  # new ones only
    task = asyncio.create_task(eng.run())
    for _ in range(100):
        await asyncio.sleep(0.02)
        if daemon_mod.socket_path().exists():
            break
    status = await client("status")
    assert (
        status
        and status["inbox"] == {"open": 2}
        and status["projects"] == [str(tmp_path.resolve())]
    )
    dg = await client("digest")
    assert "broken thing" in dg["text"]
    assert daemon_mod.read_pid() is not None
    await client("stop")
    await asyncio.wait_for(task, timeout=5)
    assert not daemon_mod.socket_path().exists() and daemon_mod.read_pid() is None


async def test_failed_run_is_filed_to_inbox(project: Path, _trendlab_home: Path):
    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    cfg.limits.max_iterations = 2
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id=f"r{i}",
                        name="read_file",
                        arguments={"path": "README.md", "start_line": 1, "end_line": i + 1},
                    )
                ]
            )
            for i in range(5)
        ]
    )
    tl = TrendLabApp = __import__("trendlab.app", fromlist=["TrendLabApp"]).TrendLabApp
    tl = TrendLabApp(
        project,
        cfg,
        provider=provider,
        model_ref="scripted:m",
        permission_mode=PermissionMode.UNSAFE,
        console=Console(quiet=True),
    )
    await tl.start(interactive=False)
    try:
        result = await tl.run_prompt("read the readme forever")
    finally:
        await tl.stop()
    assert result.status == "FAILED"
    inbox = Inbox(daemon_mod.inbox_path())
    items = inbox.list(str(project.resolve()))
    inbox.close()
    assert (
        len(items) == 1
        and items[0]["title"].startswith("run failed: read the readme")
        and items[0]["source"] == "run"
    )


def test_narrow_exclude_is_idempotent(tmp_path: Path):
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    (tmp_path / ".git/info").mkdir(exist_ok=True)
    (tmp_path / ".git/info/exclude").write_text(".trendlab/\nfoo\n")
    narrow_exclude(tmp_path)
    narrow_exclude(tmp_path)
    text = (tmp_path / ".git/info/exclude").read_text()
    assert (
        text.count("!.trendlab/skills/") == 1
        and ".trendlab/*" in text
        and "foo" in text
        and "\n.trendlab/\n" not in "\n" + text
    )


# -- IL-A observe job (evals spec §30.6) ---------------------------------------------------------

OBS_RUN = "r-20261012T053000Z-aaaa"


def _observe_engine(tmp_path: Path) -> Engine:
    from tests.test_observe import drop, notice
    from trendlab.engine import jobs

    jobs._OBSERVE_LOCK = None  # created lazily inside the running loop
    cfg = AppConfig()
    cfg.improvement.observe = True
    cfg.improvement.outbox_dir = str(tmp_path / "outbox")
    (tmp_path / "exchange").mkdir()
    cfg.improvement.ack_path = str(tmp_path / "exchange" / "observe-status.json")
    drop(tmp_path / "outbox", notice())
    return Engine(projects=[tmp_path], config=cfg, tick_seconds=0.05)


def _fake_sends(monkeypatch, results):
    from trendlab.engine import notify

    sent, it = [], iter(results)

    async def fake(config, text):
        sent.append(text)
        await asyncio.sleep(0)  # yield, so an unserialized second job could interleave
        return next(it)

    monkeypatch.setattr(notify, "send_telegram", fake)
    return sent


def test_observe_due_rules(tmp_path: Path):
    cfg = AppConfig()
    eng = Engine(projects=[tmp_path], config=cfg)
    now = datetime(2026, 10, 12, 6, 0, tzinfo=UTC)
    eng.started -= 1000
    assert not eng.due("observe", now)  # off unless configured
    cfg.improvement.observe = True
    assert eng.due("observe", now)
    eng.state["last_observe"] = datetime.now(UTC).timestamp() - 600
    assert not eng.due("observe", now)  # every 30 minutes at most
    eng.state["last_observe"] -= 1800
    assert eng.due("observe", now)
    eng.started = datetime.now(UTC).timestamp()
    assert not eng.due("observe", now)  # waits out the warm-up like the other jobs
    eng.inbox.close()


async def test_observe_job_retries_the_report_through_run_job(tmp_path: Path, monkeypatch):
    eng = _observe_engine(tmp_path)
    sent = _fake_sends(monkeypatch, [False, True])
    first = await eng.run_job("observe")
    assert "error" not in first, first
    assert first["observe"] == {"ingested": 1, "reported": [], "abandoned": [],
                                "status_written": True, "outbox_readable": True}  # fmt: skip
    second = await eng.run_job("observe")
    assert second["observe"]["ingested"] == 0 and second["observe"]["reported"] == [OBS_RUN]
    assert len(sent) == 2 and len(eng.inbox.list(status="open")) == 1
    status = json.loads((tmp_path / "exchange" / "observe-status.json").read_text())
    assert status["last_observed_run"] == OBS_RUN and status["pending_reports"] == []
    assert "last_observe" in eng.state
    eng.inbox.close()


async def test_concurrent_observe_jobs_send_at_most_one_report(tmp_path: Path, monkeypatch):
    from trendlab.engine.jobs import observe_job

    eng = _observe_engine(tmp_path)
    sent = _fake_sends(monkeypatch, [True, True])
    a, b = await asyncio.gather(
        observe_job(eng.inbox, eng.config, eng.state),
        observe_job(eng.inbox, eng.config, eng.state),
    )
    assert len(sent) == 1 and a["ingested"] + b["ingested"] == 1
    assert a["reported"] + b["reported"] == [OBS_RUN]
    eng.inbox.close()


async def test_the_digest_skips_evals_items(tmp_path: Path, monkeypatch):
    eng = Engine(projects=[tmp_path], config=AppConfig())
    sent = []

    async def fake_send(config, text):
        sent.append(text)
        return True

    monkeypatch.setattr(daemon_mod, "send_telegram", fake_send)
    for name in ("alpha", "beta", "gamma", "delta"):  # evals items that would fill a short list
        eng.inbox.record(project="/h", title=f"Evals main fail {name}", signature=name,
                         severity="high", source="evals")  # fmt: skip
    assert await eng.send_digest() is False  # the Observer report covers evals items
    eng.inbox.record(project="/p", title="broken thing", signature="x", severity="low")
    assert await eng.send_digest() is True
    assert "broken thing" in sent[0] and "Evals" not in sent[0]
    task = asyncio.create_task(eng.run())
    for _ in range(100):
        await asyncio.sleep(0.02)
        if daemon_mod.socket_path().exists():
            break
    dg = await client("digest")
    assert "broken thing" in dg["text"] and "Evals" not in dg["text"]
    await client("stop")
    await asyncio.wait_for(task, timeout=5)


def test_save_state_leaves_no_partial_file(tmp_path: Path, monkeypatch):
    eng = Engine(projects=[tmp_path], config=AppConfig())
    eng.state = {"last_watch": 1.0}
    eng._save_state()
    eng.state = {"last_watch": 2.0, "big": "x" * 10000}

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(daemon_mod.os, "replace", boom)
    try:
        eng._save_state()
    except OSError:
        pass
    assert json.loads(daemon_mod.state_path().read_text()) == {"last_watch": 1.0}
    assert sorted(p.name for p in daemon_mod.state_path().parent.iterdir()
                  if p.name.startswith(("state", ".state"))) == ["state.json"]  # fmt: skip
    eng.inbox.close()


def test_engine_run_observe_is_socket_only(tmp_path: Path, monkeypatch):
    from typer.testing import CliRunner

    from trendlab.cli import app

    async def never(self, job):
        raise AssertionError("observe must never run in-process")

    monkeypatch.setattr(Engine, "run_job", never)
    daemon_mod.pid_path().write_text(str(os.getpid()))  # even a live pid file changes nothing
    r = CliRunner().invoke(app, ["engine", "run", "observe"])
    assert r.exit_code == 1
    assert "the engine is not running; observe runs inside the engine" in r.output


async def test_observe_is_skipped_when_off_even_from_the_socket(tmp_path: Path, monkeypatch):
    eng = _observe_engine(tmp_path)
    eng.config.improvement.observe = False
    sent = _fake_sends(monkeypatch, [True])
    res = await eng.run_job("observe")
    assert res["observe"] == {"skipped": "observe is off"} and sent == []
    assert eng.inbox.notice_row(OBS_RUN) is None
    assert not (tmp_path / "exchange" / "observe-status.json").exists()
    eng.inbox.close()


async def test_an_ingest_failure_still_delivers_and_posts_the_status(tmp_path: Path, monkeypatch):
    from trendlab.engine import observe

    eng = _observe_engine(tmp_path)
    sent = _fake_sends(monkeypatch, [True])
    eng.inbox.ingest_notice(run_id="r-20261011T053000Z-zzzz", client="trendlab",
                            fingerprint="f", entries=[], payload=json.dumps(
                                {"run_id": "r-20261011T053000Z-zzzz", "run": {"status": "complete"},
                                 "counts": {}, "findings": [], "aggregates": []}))  # fmt: skip

    def boom(*a, **k):
        raise PermissionError("outbox parent not traversable")

    monkeypatch.setattr(observe, "ingest_notices", boom)
    res = (await eng.run_job("observe"))["observe"]
    assert res["ingested"] == 0 and res["outbox_readable"] is False
    assert res["reported"] == ["r-20261011T053000Z-zzzz"] and len(sent) == 1
    assert res["status_written"] is True and res["errors"][0].startswith("ingest: PermissionError")
    status = json.loads((tmp_path / "exchange" / "observe-status.json").read_text())
    assert status["outbox_readable"] is False
    eng.inbox.close()


async def test_a_delivery_failure_still_posts_the_status(tmp_path: Path, monkeypatch):
    from trendlab.engine import observe

    eng = _observe_engine(tmp_path)

    async def boom(*a, **k):
        raise RuntimeError("inbox locked")

    monkeypatch.setattr(observe, "deliver_reports", boom)
    res = (await eng.run_job("observe"))["observe"]
    assert res["ingested"] == 1 and res["status_written"] is True
    assert res["errors"] == ["deliver: RuntimeError: inbox locked"]
    status = json.loads((tmp_path / "exchange" / "observe-status.json").read_text())
    assert status["pending_reports"] == [OBS_RUN] and status["outbox_readable"] is True
    eng.inbox.close()
