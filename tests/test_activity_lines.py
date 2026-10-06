"""Every tool attempt leaves a visible trace (spec §91.1): started, finished, or skipped."""

from pathlib import Path

from rich.console import Console

from trendlab.app import TrendLabApp
from trendlab.config.loader import load_config
from trendlab.config.schema import PermissionMode, RemoteApprovalConfig
from trendlab.permissions.engine import PermissionEngine
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.telemetry.events import EventType
from trendlab.tools.base import ToolContext
from trendlab.tools.registry import default_registry
from trendlab.tools.runtime import ToolRuntime
from trendlab.ui.activity import format_event, run_footer
from trendlab.ui.repl import Repl


async def test_runtime_emits_skipped_with_reasons(project: Path, manager_factory, events, recorder):
    mgr = manager_factory()
    rt = ToolRuntime(
        default_registry(),
        PermissionEngine(PermissionMode.UNSAFE),
        mgr,
        events,
        ToolContext(project_root=project, session_id=mgr.session_id),
    )
    await rt.execute(ToolCall(id="1", name="patch_file", arguments={"path": "x", "old": "a"}))
    await rt.execute(ToolCall(id="2", name="nope_tool", arguments={}))
    await rt.execute(ToolCall(id="3", name="shell", arguments={"command": "sudo rm -rf /"}))
    await rt.execute(ToolCall(id="4", name="_malformed", arguments={"error": "bad json"}))
    await rt.execute(ToolCall(id="5", name="read_file", arguments={"path": "src/app.py"}))
    await rt.execute(ToolCall(id="6", name="shell", arguments={"command": "ls -la src && false"}))
    skipped = [(e.data["tool"], e.data["reason"]) for e in recorder.of_type(EventType.TOOL_SKIPPED)]
    assert skipped == [
        ("patch_file", "invalid_args"),
        ("nope_tool", "unknown_tool"),
        ("shell", "denied"),
        ("_malformed", "malformed"),
    ]
    inv = recorder.of_type(EventType.TOOL_SKIPPED)[0].data
    assert "old_text" in inv["detail"] and inv["message"].startswith("invalid arguments")
    started = {e.data["tool"]: e.data for e in recorder.of_type(EventType.TOOL_STARTED)}
    assert started["read_file"]["detail"] == "src/app.py"
    assert started["shell"]["detail"] == "$ ls -la src && false" and started["shell"]["command"]
    done = {e.data["tool"]: e.data for e in recorder.of_type(EventType.TOOL_COMPLETED)}
    assert (
        done["read_file"]["ok"]
        and done["read_file"]["lines"] >= 1
        and done["read_file"]["preview"] == ""
    )
    assert not done["shell"]["ok"] and done["shell"]["exit_code"] == 1 and done["shell"]["error"]


def test_format_event_lines(recorder, events):
    events.emit(
        EventType.TOOL_STARTED,
        session_id="s",
        tool="shell",
        summary="Run shell command",
        detail="$ pytest -q",
        command="pytest -q",
        files=[],
    )
    events.emit(
        EventType.TOOL_COMPLETED,
        session_id="s",
        tool="shell",
        ok=True,
        duration_ms=120,
        lines=3,
        preview="3 passed",
    )
    events.emit(
        EventType.TOOL_COMPLETED,
        session_id="s",
        tool="shell",
        ok=False,
        duration_ms=5,
        exit_code=2,
        error="No such file",
        lines=1,
    )
    events.emit(
        EventType.TOOL_SKIPPED,
        session_id="s",
        tool="shell",
        reason="denied",
        detail="$ sudo ls",
        message="DENIED: privileged",
    )
    events.emit(
        EventType.DIAGNOSTICS,
        session_id="s",
        files=["a.py"],
        report="Diagnostics after edit:\na.py:1: E999 bad\n",
    )
    events.emit(EventType.TOOLS_PARALLEL, session_id="s", count=3)
    lines = [format_event(e) for e in recorder.events]
    assert "Run shell command" in lines[0] and "$ pytest -q" in lines[0]
    assert (
        "✓" in lines[1]
        and "120 ms" in lines[1]
        and "3 lines" in lines[1]
        and "3 passed" in lines[1]
    )
    assert "✗" in lines[2] and "exit 2" in lines[2] and "No such file" in lines[2]
    assert "denied" in lines[3] and "sudo ls" in lines[3] and "privileged" in lines[3]
    assert "diagnostics" in lines[4] and "E999" in lines[4]
    assert "3 read-only tools in parallel" in lines[5]
    assert (
        format_event(
            recorder.events[0].__class__(type=EventType.MODEL_CALL_STARTED, session_id="s", data={})
        )
        is None
    )


def test_run_footer():
    from trendlab.agent.runtime import RunResult

    ok = RunResult(
        status="COMPLETED",
        text="x",
        report="",
        changed_files=["a.py"],
        validation_runs=[{"ok": True}],
        cost_usd=0.0042,
        elapsed_s=12.3,
        model_calls=3,
    )
    line = run_footer(ok)
    assert (
        "✓ done" in line
        and "3 calls" in line
        and "12s" in line
        and "$0.004" in line
        and "a.py" in line
        and "validated ✓" in line
    )
    bad = RunResult(
        status="FAILED",
        text="",
        report="",
        stop_reason="cost limit",
        changed_files=["a.py"],
        cost_usd=1.0,
        elapsed_s=1,
        model_calls=9,
    )
    line = run_footer(bad)
    assert "✗ stopped" in line and "cost limit" in line and "not validated" in line


async def test_plain_repl_shows_tool_activity_and_footer(project: Path, _trendlab_home: Path):
    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="1", name="shell", arguments={"command": "sudo whoami"}),
                    ToolCall(id="2", name="read_file", arguments={"path": "src/app.py"}),
                ]
            ),
            ModelResponse(text="TIMEOUT is 30; nothing changed"),
        ]
    )
    console = Console(record=True, width=120, force_terminal=False)
    tl = TrendLabApp(
        project,
        cfg,
        provider=provider,
        model_ref="scripted:m",
        permission_mode=PermissionMode.UNSAFE,
        console=console,
    )
    repl = Repl(tl, console)
    await tl.start(interactive=False)
    try:
        tl.events.subscribe(repl._on_event)
        await repl._prompt("what is the timeout?")
        out = console.export_text()
        assert "denied" in out and "sudo whoami" in out  # the refused call is visible
        assert "Read src/app.py" in out and "✓ read_file" in out
        assert "TIMEOUT is 30" in out and "✓ done" in out and "2 calls" in out
    finally:
        await tl.stop()
