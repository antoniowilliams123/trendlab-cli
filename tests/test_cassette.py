"""Uplift U20: cassette recording and deterministic replay."""

from pathlib import Path

from rich.console import Console

from trendlab.app import TrendLabApp
from trendlab.benchmarks.cassette import deterministic_replay, load
from trendlab.config.loader import load_config, trendlab_home
from trendlab.config.schema import PermissionMode, RemoteApprovalConfig
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.sessions.store import SessionStore


def _call(i, name, **args):
    return ModelResponse(tool_calls=[ToolCall(id=str(i), name=name, arguments=args)])


async def _record(project: Path) -> str:
    cfg = load_config(project)
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    provider = ScriptedProvider(
        [
            _call(1, "read_file", path="src/app.py"),
            _call(2, "write_file", path="src/app.py", content="TIMEOUT = 60\n"),
            _call(3, "shell", command="python3 -c 'print(\"ran ok\")'"),
            ModelResponse(text="Raised the timeout to 60."),
        ]
    )
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
        await tl.run_prompt("raise the timeout to 60")
        return tl.session_id
    finally:
        await tl.stop()


async def test_session_is_recorded_and_replays_identically(project: Path, _trendlab_home: Path):
    sid = await _record(project)
    tape = load(sid)
    assert [r["role"] for r in tape if "role" in r] == ["main"] * 4
    taped = [r for r in tape if "tool" in r]  # the shell result, raw, with its exit code
    assert [r["tool"] for r in taped] == ["shell"] and taped[0]["exit_code"] == 0
    assert "ran ok" in taped[0]["output"]
    assert (project / "src/app.py").read_text() == "TIMEOUT = 60\n"
    store = SessionStore(trendlab_home() / "sessions.db")
    try:
        cfg = load_config(project)
        cfg.remote_approval = RemoteApprovalConfig(enabled=False)
        report = await deterministic_replay(store, sid, config=cfg)
    finally:
        store.close()
    assert report["identical"], report
    assert report["model_calls_served"] == report["model_calls_recorded"] == 4
    assert report["tool_calls_before"] == report["tool_calls_after"] == 3
    assert report["files_restored"] == 1 and report["outcomes_after"] == ["completed"]
    assert report["cost"] == 0.0
    assert (project / "src/app.py").read_text() == "TIMEOUT = 60\n"  # the real project untouched


async def test_a_harness_change_shows_up_as_divergence(project: Path, _trendlab_home: Path):
    sid = await _record(project)
    store = SessionStore(trendlab_home() / "sessions.db")
    try:
        cfg = load_config(project)
        cfg.remote_approval = RemoteApprovalConfig(enabled=False)
        cfg.governance.max_answer_words = 2  # a new rule asks for a rewrite → one more call
        report = await deterministic_replay(store, sid, config=cfg)
    finally:
        store.close()
    assert not report["identical"]
    assert any("no more answers" in d for d in report["divergences"])


async def test_no_cassette_is_reported(_trendlab_home: Path):
    store = SessionStore(trendlab_home() / "sessions.db")
    try:
        report = await deterministic_replay(store, "nope", config=load_config())
    finally:
        store.close()
    assert report["error"].startswith("no cassette")


def test_cassettes_follow_session_retention(_trendlab_home: Path):
    from trendlab.benchmarks.cassette import cassette_dir, prune_cassettes

    store = SessionStore(trendlab_home() / "sessions.db")
    keep = store.create_session("/p", "m", "m")
    cassette_dir().mkdir(parents=True, exist_ok=True)
    (cassette_dir() / f"{keep}.jsonl").write_text("{}\n")
    (cassette_dir() / "gone.jsonl").write_text("{}\n")
    assert prune_cassettes(store) == 1
    assert (cassette_dir() / f"{keep}.jsonl").exists()
    store.close()
