"""Evaluation layer (cheap-model spec §8): suite tasks, profiles, docker sandbox argv, stub
server, shadow replay."""

import json
import subprocess
import urllib.request
from pathlib import Path

from trendlab.benchmarks import suite as suite_mod
from trendlab.benchmarks.replay import ReplayTools, canonical, load_session, replay_session
from trendlab.benchmarks.runner import (
    _changed_lines,
    apply_profile,
    compare_summaries,
    run_task,
    select_tasks,
    summarize,
)
from trendlab.config.schema import AppConfig, ProviderConfig, SandboxConfig
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.security.sandbox import Sandbox
from trendlab.tools.stubs import StubServer, example_for, routes_from_openapi


def test_suite_shape_and_a_python_task_round_trips(tmp_path: Path):
    tasks = suite_mod.TASKS
    assert len(tasks) == 86 and len({t.id for t in tasks}) == 86  # + 5 jailbreak (U11)
    assert sum(1 for t in tasks if t.tier == "hard") == 10 and all(
        t.lang == "python" for t in tasks if t.tier == "hard"
    )
    langs = {t.lang for t in tasks}
    assert langs == {"python", "typescript", "go"}
    assert (
        sum(1 for t in tasks if "existing tests still pass" in t.prompt) >= 20
    )  # localisation-weighted
    task = suite_mod.get_task("py01-off_by_one")
    root = tmp_path / "repo"
    prompt = suite_mod.materialize(task, root)
    assert "Symptom" in prompt and task.answer_file == "shop/util.py" and task.answer_line > 0
    assert (
        (root / "shop/util.py")
        .read_text()
        .splitlines()[task.answer_line - 1]
        .strip()
        .startswith("return [items[i : i + size] for i in range(0, len(items) - 1, size)]")
    )
    r = subprocess.run(task.test_command, shell=True, cwd=root, capture_output=True, text=True)
    assert r.returncode != 0  # visible failure for this task
    suite_mod.write_hidden_test(task, root)
    assert (root / task.defect.hidden_test_file).is_file()
    assert (
        select_tasks("3") == tasks[:3]
        and len(select_tasks(None, None, "hard")) == 8
        and len(select_tasks(None, None, "all")) == 76  # non-holdout, incl. 5 jailbreak
        and [t.id for t in select_tasks("py01,ts01")]
        == [
            "py01-off_by_one",
            "ts01-off_by_one",
        ]
    )
    assert (
        all(t.lang == "go" for t in select_tasks(None, "go")) and len(select_tasks(None, "go")) == 8
    )


def test_profiles_and_summaries():
    cfg = apply_profile(AppConfig(), "bare")
    assert cfg.planner.enabled is False and cfg.verification.verifier == "off"
    assert (
        cfg.attempts.best_of == 1
        and cfg.prompts.drivers is False
        and cfg.context.tool_budgets == {}
    )
    assert apply_profile(AppConfig(), "harness").planner.enabled is True
    assert apply_profile(AppConfig(), "x:y@bare").prompts.drivers is False
    assert _changed_lines("a\nb\nc\n", "a\nB\nc\n") == {2}
    assert _changed_lines("a\nb\n", "a\nb\nc\n") == {3}
    rows = [
        {
            "located": True,
            "root_cause": True,
            "passes": True,
            "no_collateral": True,
            "regression_added": False,
            "cost": 0.01,
            "tokens_lead": 100,
            "wall_s": 5,
            "cost_by_phase": {"edit": 0.01},
        },
        {
            "located": False,
            "root_cause": False,
            "passes": False,
            "no_collateral": True,
            "regression_added": True,
            "cost": 0.02,
            "tokens_lead": 300,
            "wall_s": 7,
            "cost_by_phase": {"edit": 0.02},
        },
        {"task": "go01", "skipped": "go not installed"},
    ]
    s = summarize(rows)
    assert s["tasks"] == 2 and s["skipped"] == 1 and s["located"] == 0.5 and s["cost"] == 0.03
    assert s["cost_by_phase"] == {"edit": 0.03}
    d = compare_summaries(s, {**s, "passes": 1.0, "cost": 0.05})
    assert d["passes"] == 0.5 and d["cost"] == 0.02


async def test_run_task_scores_a_scripted_fix(_trendlab_home: Path):
    task = suite_mod.get_task("py01-off_by_one")
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="1", name="run_tests", arguments={"kind": "test"})]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="2",
                        name="patch_file",
                        arguments={
                            "path": "shop/util.py",
                            "old_text": "len(items) - 1, size",
                            "new_text": "len(items), size",
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="3",
                        name="write_file",
                        arguments={
                            "path": "tests/test_regress.py",
                            "content": "from shop.util import chunks\n\n\ndef test_tail():\n    assert chunks([1, 2, 3], 2) == [[1, 2], [3]]\n",
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[ToolCall(id="4", name="run_tests", arguments={"kind": "test"})]
            ),
            ModelResponse(text="Fixed the off-by-one in chunks(); tests pass."),
        ]
    )
    cfg = AppConfig(providers={"scripted": ProviderConfig()})
    cfg.remote_approval.enabled = False
    r = await run_task(
        task, "scripted:m", config=cfg, provider=provider, home=_trendlab_home, profile="bare"
    )
    assert (
        r["located"]
        and r["root_cause"]
        and r["passes"]
        and r["no_collateral"]
        and r["regression_added"]
    )
    assert r["status"] == "COMPLETED" and r["interventions"] == 0 and r["tokens_lead"] == 0
    assert set(r["cost_by_phase"]) <= {"plan", "explore", "edit", "validate", "other", "verify"}
    go = suite_mod.get_task("go01-off_by_one")
    import shutil

    if shutil.which("go") is None:
        skipped = await run_task(
            go, "scripted:m", config=cfg, provider=provider, home=_trendlab_home
        )
        assert skipped["skipped"].startswith("go not installed")


def test_docker_sandbox_argv(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TRENDLAB_SANDBOX", "docker")
    sb = Sandbox(
        SandboxConfig(mode="docker", docker_image="python:3.12-slim"),
        tmp_path,
        docker="/usr/bin/docker",
    )
    argv = sb.wrap("pytest -q", cwd=tmp_path / "sub")
    assert argv[:4] == ["/usr/bin/docker", "run", "--rm", "-i"]
    assert "--network" in argv and "none" in argv
    assert f"{tmp_path.resolve()}:/work" in argv and "/work/sub" in argv
    assert "python:3.12-slim" in argv and argv[-3:] == ["/bin/sh", "-c", "pytest -q"]
    sb_net = Sandbox(
        SandboxConfig(mode="docker", allow_network=True), tmp_path, docker="/usr/bin/docker"
    )
    assert "--network" not in sb_net.wrap("ls", cwd=tmp_path)
    none = Sandbox(SandboxConfig(mode="docker"), tmp_path, docker=None)
    assert not none.available


def test_stub_server_openapi_recording_and_404(tmp_path: Path):
    spec = {
        "paths": {
            "/users/{id}": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "integer"},
                                            "name": {"type": "string", "example": "ann"},
                                        },
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/orders": {
                "post": {
                    "responses": {
                        "201": {"content": {"application/json": {"example": {"ok": True}}}}
                    }
                }
            },
        }
    }
    assert example_for({"type": "array", "items": {"type": "integer"}}) == [0]
    routes = routes_from_openapi(spec)
    assert len(routes) == 2 and routes[0][2] == 200
    path = tmp_path / "api.json"
    path.write_text(json.dumps(spec))
    server = StubServer(spec=path).start()
    try:
        with urllib.request.urlopen(f"{server.url}/users/7") as resp:
            assert resp.status == 200 and json.loads(resp.read()) == {"id": 0, "name": "ann"}
        req = urllib.request.Request(f"{server.url}/orders", data=b"{}", method="POST")
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 201 and json.loads(resp.read()) == {"ok": True}
        try:
            urllib.request.urlopen(f"{server.url}/nope")
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.stop()
    rec = tmp_path / "rec.json"
    rec.write_text(
        json.dumps(
            {"exchanges": [{"method": "GET", "path": "/ping", "status": 200, "body": {"pong": 1}}]}
        )
    )
    server = StubServer(spec=rec).start()
    try:
        with urllib.request.urlopen(f"{server.url}/ping") as resp:
            assert json.loads(resp.read()) == {"pong": 1}
    finally:
        server.stop()


async def test_replay_feeds_recorded_results_and_reports(project: Path, _trendlab_home: Path):
    from trendlab.config.loader import load_config, trendlab_home
    from trendlab.sessions.store import SessionStore

    (project / "src/a.py").write_text("x = 1\n")
    store = SessionStore(trendlab_home() / "sessions.db")
    sid = (
        store.create_session(str(project), "scripted:m")["id"]
        if isinstance(store.create_session(str(project), "scripted:m"), dict)
        else None
    )
    if sid is None:
        sid = store.latest_session(str(project))["id"]
    store.append_message(sid, {"role": "user", "content": "what is in src/a.py?"})
    store.append_message(
        sid,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "src/a.py"}),
                    },
                }
            ],
        },
    )
    store.append_message(
        sid,
        {
            "role": "tool",
            "tool_call_id": "c1",
            "name": "read_file",
            "content": "1\tx = 1  # RECORDED",
        },
    )
    store.append_message(sid, {"role": "assistant", "content": "It sets x to 1."})
    rec = load_session(store, sid)
    assert rec.prompts == ["what is in src/a.py?"] and rec.tool_calls == [
        canonical("read_file", {"path": "src/a.py"})
    ]
    assert rec.assistant_turns == 2 and "RECORDED" in rec.results[rec.tool_calls[0]]
    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    cfg.remote_approval.enabled = False
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="n1", name="read_file", arguments={"path": "src/a.py"})]
            ),
            ModelResponse(
                tool_calls=[ToolCall(id="n2", name="list_directory", arguments={"path": "src"})]
            ),
            ModelResponse(text="x is 1; src has one file"),
        ]
    )
    report = await replay_session(
        store, sid, config=cfg, model="scripted:m", provider=provider, home=_trendlab_home
    )
    store.close()
    assert (
        report["outcomes"] == ["COMPLETED"]
        and report["tool_calls_before"] == 1
        and report["tool_calls_after"] == 2
    )
    assert (
        report["replayed_results"] == 1
        and report["live_calls"] == 1
        and report["tool_sequence_shared_prefix"] == 1
    )
    assert report["tools_after"] == {"list_directory": 1, "read_file": 1}
    assert isinstance(ReplayTools, type)


def test_replay_copy_refuses_home_and_uses_tracked_files(tmp_path: Path):
    from trendlab.benchmarks.replay import copy_project

    assert "home directory" in copy_project(Path.home(), tmp_path / "x")
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "-C", str(src), "init", "-q"], check=True)
    (src / "a.py").write_text("x = 1\n")
    (src / ".gitignore").write_text("big.bin\n")
    (src / "big.bin").write_bytes(b"0" * 10)
    assert copy_project(src, tmp_path / "dest") is None
    assert (tmp_path / "dest/a.py").is_file() and not (tmp_path / "dest/big.bin").exists()


def test_working_set_copy_only_takes_touched_files(tmp_path: Path):
    from trendlab.benchmarks.replay import RecordedSession, copy_working_set, working_set

    src = tmp_path / "home"
    (src / "proj").mkdir(parents=True)
    (src / "proj/a.py").write_text("a = 1\n")
    (src / "proj/b.py").write_text("b = 1\n")
    (src / "big").mkdir()
    (src / "big/data.bin").write_bytes(b"0" * 100)
    rec = RecordedSession(
        session_id="s",
        project_path=str(src),
        model="m",
        prompts=["p"],
        tool_calls=[
            ("read_file", json.dumps({"path": "proj/a.py"})),
            ("read_file", json.dumps({"file_path": str(src / "proj/b.py")})),
            ("web_fetch", json.dumps({"url": "https://x"})),
            ("read_file", json.dumps({"path": "/etc/passwd"})),
        ],
    )
    assert working_set(rec) == {"proj/a.py", str(src / "proj/b.py"), "/etc/passwd"}
    n = copy_working_set(src, tmp_path / "dest", rec)
    assert (
        n == 2
        and (tmp_path / "dest/proj/a.py").is_file()
        and (tmp_path / "dest/proj/b.py").is_file()
    )
    assert not (tmp_path / "dest/big").exists() and not (tmp_path / "dest/etc").exists()
