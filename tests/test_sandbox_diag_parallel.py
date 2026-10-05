import asyncio
import shutil
from pathlib import Path

import pytest

from trendlab.config.schema import (
    AppConfig,
    DiagnosticsConfig,
    LimitsConfig,
    PermissionMode,
    SandboxConfig,
)
from trendlab.permissions.engine import PermissionEngine
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.security.sandbox import Sandbox
from trendlab.telemetry.events import EventType
from trendlab.tools.base import ToolContext
from trendlab.tools.diagnostics import Diagnostics
from trendlab.tools.registry import default_registry
from trendlab.tools.runtime import ToolRuntime

from .test_agent_runtime import make_agent

HAS_BWRAP = shutil.which("bwrap") is not None


def test_sandbox_wrap_shape(project: Path, monkeypatch):
    monkeypatch.delenv("TRENDLAB_SANDBOX", raising=False)
    sb = Sandbox(SandboxConfig(mode="on", writable_paths=[]), project, bwrap="/usr/bin/bwrap")
    argv = sb.wrap("echo hi", cwd=project)
    assert argv[0] == "/usr/bin/bwrap" and argv[-3:] == ["/bin/sh", "-c", "echo hi"]
    assert "--unshare-net" in argv and "--die-with-parent" in argv
    binds = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
    assert str(project.resolve()) in binds
    assert argv.index("--tmpfs") < argv.index("--bind")
    net = Sandbox(
        SandboxConfig(mode="on", allow_network=True, writable_paths=[]),
        project,
        bwrap="/usr/bin/bwrap",
    )
    assert "--unshare-net" not in net.wrap("x", cwd=project)
    off = Sandbox(SandboxConfig(mode="off"), project, bwrap="/usr/bin/bwrap")
    assert off.wrap("echo", cwd=project) == ["/bin/sh", "-c", "echo"] and off.describe() == "off"
    missing = Sandbox(SandboxConfig(mode="auto"), project, bwrap=None)
    assert not missing.active and "unavailable" in missing.describe()
    with pytest.raises(RuntimeError, match="bwrap"):
        _ = Sandbox(SandboxConfig(mode="on"), project, bwrap=None).active
    monkeypatch.setenv("TRENDLAB_SANDBOX", "off")
    assert not Sandbox(SandboxConfig(mode="on"), project, bwrap="/usr/bin/bwrap").active


@pytest.mark.skipif(not HAS_BWRAP, reason="bubblewrap not installed")
async def test_sandbox_blocks_writes_outside_project_and_network(
    project: Path, manager_factory, events, tmp_path, monkeypatch
):
    monkeypatch.setenv("TRENDLAB_SANDBOX", "on")
    mgr = manager_factory()
    sb = Sandbox(SandboxConfig(mode="on", writable_paths=[]), project)
    ctx = ToolContext(project_root=project, session_id=mgr.session_id, sandbox=sb)
    rt = ToolRuntime(default_registry(), PermissionEngine(PermissionMode.UNSAFE), mgr, events, ctx)
    outside = tmp_path / "outside.txt"
    res = await rt.execute(
        ToolCall(
            id="1",
            name="shell",
            arguments={"command": f"touch {outside} 2>&1; touch ~/.tl_sbx_probe 2>&1; echo done"},
        )
    )
    assert (
        "done" in res.output
        and not outside.exists()
        and not (Path.home() / ".tl_sbx_probe").exists()
    )
    res = await rt.execute(
        ToolCall(id="2", name="shell", arguments={"command": "touch inside.txt && echo ok"})
    )
    assert "ok" in res.output and (project / "inside.txt").exists()
    res = await rt.execute(
        ToolCall(
            id="3",
            name="shell",
            arguments={
                "command": "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), timeout=2)\" 2>&1 | tail -1; echo net=$?"
            },
        )
    )
    assert "net=0" in res.output and (
        "unreachable" in res.output.lower() or "error" in res.output.lower()
    )


async def test_diagnostics_feed_back_into_tool_result(
    project: Path, manager_factory, events, recorder, tmp_path
):
    mgr = manager_factory()
    linter = tmp_path / "lint.sh"
    linter.write_text(
        '#!/bin/sh\ncase "$1" in *bad*) echo "$1:1: E999 bad code"; exit 1;; *) exit 0;; esac\n'
    )
    linter.chmod(0o755)
    cfg = DiagnosticsConfig(commands={".py": [f"{linter} {{files}}"]})
    diag = Diagnostics(cfg, project)
    assert diag.commands_for(["a.py", "b.txt"]) == [f"{linter} a.py"]
    assert await diag.run(["good.py"]) == ""
    report = await diag.run(["bad.py"])
    assert report.startswith("Diagnostics after edit") and "E999 bad code" in report
    rt = ToolRuntime(
        default_registry(),
        PermissionEngine(PermissionMode.UNSAFE),
        mgr,
        events,
        ToolContext(project_root=project, session_id=mgr.session_id),
    )
    rt.diagnostics = diag
    res = await rt.execute(
        ToolCall(id="1", name="write_file", arguments={"path": "bad.py", "content": "x =\n"})
    )
    assert res.ok and "E999 bad code" in res.output and "E999" in res.data["diagnostics"]
    assert recorder.of_type(EventType.DIAGNOSTICS)[0].data["files"] == ["bad.py"]
    res = await rt.execute(
        ToolCall(id="2", name="write_file", arguments={"path": "good.py", "content": "x = 1\n"})
    )
    assert res.ok and "Diagnostics" not in res.output
    assert Diagnostics(DiagnosticsConfig(enabled=False), project).commands_for(["a.py"]) == []
    # Missing linters are skipped quietly.
    assert (
        Diagnostics(
            DiagnosticsConfig(commands={".py": ["definitely-not-a-linter {files}"]}), project
        ).commands_for(["a.py"])
        == []
    )


async def test_parallel_read_only_tools_run_concurrently(
    project, manager_factory, events, recorder
):
    mgr = manager_factory()
    calls = [
        ToolCall(id=str(i), name="read_file", arguments={"path": "src/app.py"}) for i in range(4)
    ]
    calls.append(ToolCall(id="w", name="write_file", arguments={"path": "o.txt", "content": "x"}))
    calls += [
        ToolCall(id="g", name="glob", arguments={"pattern": "*.md"}),
        ToolCall(id="l", name="list_directory", arguments={"path": "."}),
    ]
    provider = ScriptedProvider(
        [ModelResponse(tool_calls=calls), ModelResponse(text="done; validation not possible")]
    )
    agent, tools = make_agent(
        project,
        mgr,
        events,
        provider,
        mode=PermissionMode.UNSAFE,
        config=AppConfig(limits=LimitsConfig(parallel_tools=3)),
    )
    started: list[str] = []
    orig = tools.execute

    async def tracking(call):
        started.append(call.id)
        await asyncio.sleep(0.01)
        return await orig(call)

    tools.execute = tracking
    result = await agent.run("go")
    assert result.status == "COMPLETED"
    groups = [e.data for e in recorder.of_type(EventType.TOOLS_PARALLEL)]
    assert [g["count"] for g in groups] == [
        3,
        2,
    ]  # 4 reads → 3 + 1 (sequential single), then glob+ls
    tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == [
        "0",
        "1",
        "2",
        "3",
        "w",
        "g",
        "l",
    ]  # order preserved
    assert (project / "o.txt").exists()
