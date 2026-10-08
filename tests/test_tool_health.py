"""Uplift U4: tool counters, circuit breaker, postconditions, web_fetch retry, /tools."""

from pathlib import Path

from trendlab.config.schema import PermissionMode
from trendlab.permissions.engine import PermissionEngine
from trendlab.providers.base import ToolCall
from trendlab.telemetry.events import EventType
from trendlab.tools.base import ToolContext
from trendlab.tools.health import ToolHealth, postcondition
from trendlab.tools.registry import default_registry
from trendlab.tools.runtime import ToolRuntime


def test_breaker_opens_after_consecutive_failures_and_half_opens():
    h = ToolHealth(threshold=3, cooldown_s=60)
    for _ in range(2):
        assert not h.record(
            "web_fetch", ok=False, failed=True, skipped=False, ms=10, error="boom", now=0.0
        )
    assert h.blocked("web_fetch", now=1.0) is None
    assert h.record("web_fetch", ok=False, failed=True, skipped=False, ms=10, error="boom", now=2.0)
    msg = h.blocked("web_fetch", now=3.0)
    assert msg and "paused" in msg and "web_search" in msg
    assert h.blocked("web_fetch", now=70.0) is None  # half-open probe allowed
    assert not h.record("web_fetch", ok=True, failed=False, skipped=False, ms=5, now=71.0)
    assert (
        h.stats["web_fetch"].consecutive_failures == 0
        and h.summary()["web_fetch"]["breaker_opened"] == 1
    )
    # a non-zero exit from shell is a result, not a failure
    h.record("shell", ok=False, failed=False, skipped=False, ms=10)
    assert h.stats["shell"].consecutive_failures == 0 and h.overall_success_rate() < 1.0


def test_postconditions_catch_unparsable_files(tmp_path: Path):
    py = tmp_path / "a.py"
    py.write_text("def f(:\n    pass\n")
    assert "a.py:1" in postcondition(py)
    py.write_text("def f():\n    pass\n")
    assert postcondition(py) is None
    js = tmp_path / "x.json"
    js.write_text("{bad")
    assert postcondition(js) and "x.json" in postcondition(js)
    assert postcondition(tmp_path / "notes.md") is None


async def test_runtime_pauses_tool_and_reports_postcondition(
    project: Path, manager_factory, events, recorder
):
    mgr = manager_factory()
    rt = ToolRuntime(
        default_registry(),
        PermissionEngine(PermissionMode.UNSAFE),
        mgr,
        events,
        ToolContext(project_root=project, session_id=mgr.session_id),
    )
    rt.health.threshold = 2
    # force two tool errors on list_directory by pointing it at a file path that raises
    (project / "f.txt").write_text("x")
    bad = ToolCall(id="1", name="read_file", arguments={"path": "missing.py"})
    r1 = await rt.execute(bad)
    assert (
        not r1.ok and rt.health.stats["read_file"].consecutive_failures == 0
    )  # 'not a file' is a result
    # make read_file raise: a directory path with a binary trick is awkward; use health directly
    rt.health.record(
        "search_text", ok=False, failed=True, skipped=False, ms=1, error="tool error: rg crashed"
    )
    rt.health.record(
        "search_text", ok=False, failed=True, skipped=False, ms=1, error="tool error: rg crashed"
    )
    res = await rt.execute(ToolCall(id="2", name="search_text", arguments={"pattern": "x"}))
    assert not res.ok and res.output.startswith("PAUSED:") and "glob" in res.output
    assert recorder.of_type(EventType.TOOL_SKIPPED)[-1].data["reason"] == "circuit_open"
    # postcondition: an edit that breaks Python syntax is flagged
    w = await rt.execute(
        ToolCall(
            id="3",
            name="write_file",
            arguments={"path": "src/broken.py", "content": "def f(:\n  pass\n"},
        )
    )
    assert w.ok and "POSTCONDITION FAILED" in w.output and "broken.py:1" in w.output
    assert recorder.of_type(EventType.TOOL_POSTCONDITION_FAILED)[0].data["files"] == [
        "src/broken.py"
    ]
    good = await rt.execute(
        ToolCall(id="4", name="write_file", arguments={"path": "src/ok.py", "content": "x = 1\n"})
    )
    assert "POSTCONDITION" not in good.output and rt.health.stats["write_file"].ok == 2


async def test_git_tools_hidden_outside_a_repository(
    tmp_path: Path, manager_factory, events, recorder
):
    import subprocess

    plain = tmp_path / "plain"
    plain.mkdir()
    mgr = manager_factory()
    rt = ToolRuntime(
        default_registry(),
        PermissionEngine(PermissionMode.UNSAFE),
        mgr,
        events,
        ToolContext(project_root=plain, session_id=mgr.session_id),
    )
    names = {s["function"]["name"] for s in rt.visible_schemas()}
    assert "git_status" not in names and "read_file" in names
    res = await rt.execute(ToolCall(id="g", name="git_status", arguments={}))
    assert (
        not res.ok
        and res.output.startswith("UNAVAILABLE: git_status")
        and "not a git repository" in res.output
    )
    assert recorder.of_type(EventType.TOOL_SKIPPED)[-1].data["reason"] == "unavailable_here"
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    rt.ctx.project_root = repo / "sub"  # a subdirectory of a repo still counts
    assert "git_status" in {s["function"]["name"] for s in rt.visible_schemas()}


async def test_paused_run_tests_falls_back_to_shell(
    project: Path, manager_factory, events, recorder
):
    import time

    from trendlab.config.schema import PermissionMode
    from trendlab.permissions.engine import PermissionEngine
    from trendlab.providers.base import ToolCall
    from trendlab.telemetry.events import EventType
    from trendlab.tools.base import ToolContext
    from trendlab.tools.registry import default_registry
    from trendlab.tools.runtime import ToolRuntime

    mgr = manager_factory()
    rt = ToolRuntime(
        default_registry(),
        PermissionEngine(PermissionMode.UNSAFE),
        mgr,
        events,
        ToolContext(
            project_root=project,
            session_id=mgr.session_id,
            validation_commands={"test": "python3 -c 'print(\"suite ok\")'"},
        ),
    )
    s = rt.health._s("run_tests")
    s.open_until, s.consecutive_failures, s.last_error = time.monotonic() + 60, 3, "boom"
    r = await rt.execute(ToolCall(id="t", name="run_tests", arguments={"kind": "test"}))
    assert r.ok and r.output.startswith("[run_tests is paused; ran it through shell instead]")
    assert "suite ok" in r.output
    rec = [e.data for e in recorder.of_type(EventType.RECOVERY)]
    assert rec and rec[-1]["action"] == "tool_fallback" and rec[-1]["fallback"] == "shell"
    # no fallback exists for this tool: it is skipped with the readable reason
    s2 = rt.health._s("web_fetch")
    s2.open_until, s2.consecutive_failures, s2.last_error = time.monotonic() + 60, 3, "x"
    r2 = await rt.execute(ToolCall(id="w", name="web_fetch", arguments={"url": "https://e.com"}))
    assert not r2.ok and "PAUSED" in r2.output


async def test_latency_budget_nudges_once(project: Path, manager_factory, events, recorder):
    from trendlab.config.schema import AppConfig, PermissionMode
    from trendlab.providers.base import ModelResponse, ToolCall
    from trendlab.providers.scripted import ScriptedProvider
    from trendlab.telemetry.events import EventType

    from .test_agent_runtime import make_agent

    cfg = AppConfig()
    cfg.limits.latency_budget_s = {"question": 0.000001, "small_fix": 0.000001}
    reads = [
        ModelResponse(
            tool_calls=[ToolCall(id=str(i), name="read_file", arguments={"path": "src/app.py"})]
        )
        for i in range(3)
    ]
    agent, _ = make_agent(
        project,
        manager_factory(),
        events,
        ScriptedProvider([*reads, ModelResponse(text="TIMEOUT is 30.")]),
        mode=PermissionMode.UNSAFE,
        config=cfg,
    )
    result = await agent.run("what is the timeout?")
    warns = [
        e.data
        for e in recorder.of_type(EventType.BUDGET_WARNING)
        if e.data.get("kind") == "latency"
    ]
    assert len(warns) == 1 and warns[0]["budget_s"] == 0.000001
    nudges = [m for m in agent.messages if "Time check:" in str(m.get("content"))]
    assert len(nudges) == 1
    assert result.latency == {"budget_s": 0.000001, "over": True}
