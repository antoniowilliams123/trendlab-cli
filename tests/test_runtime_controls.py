"""Uplift U26: run lock, tool allowlist, per-run token cap, verification latency."""

import json
import os
from pathlib import Path

import pytest

from trendlab.benchmarks.runner import verification_latency
from trendlab.providers.base import ModelResponse, TokenUsage, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.sessions.runlock import RunLocked, acquire, lock_path, release


def test_run_lock_refuses_a_second_live_run(tmp_path: Path):
    acquire(tmp_path, "s1")
    acquire(tmp_path, "s1")  # the same process may re-enter
    lock_path(tmp_path).write_text(json.dumps({"pid": 1, "session": "other", "started": "x"}))
    with pytest.raises(RunLocked) as exc:  # pid 1 is always alive
        acquire(tmp_path, "s2")
    assert "--worktree" in str(exc.value)
    lock_path(tmp_path).write_text(json.dumps({"pid": 999_999_9, "session": "dead"}))
    acquire(tmp_path, "s3")  # a dead holder's lock is taken over
    assert json.loads(lock_path(tmp_path).read_text())["pid"] == os.getpid()
    release(tmp_path)
    assert not lock_path(tmp_path).exists()


async def test_tool_allowlist(project: Path, manager_factory, events):
    from trendlab.config.schema import PermissionMode
    from trendlab.permissions.engine import PermissionEngine
    from trendlab.tools.base import ToolContext
    from trendlab.tools.registry import default_registry
    from trendlab.tools.runtime import ToolRuntime

    mgr = manager_factory()
    rt = ToolRuntime(
        default_registry(),
        PermissionEngine(PermissionMode.UNSAFE),
        mgr,
        events,
        ToolContext(project_root=project, session_id=mgr.session_id),
    )
    rt.allow_tools = {"read_file", "search_text"}
    names = {s["function"]["name"] for s in rt.visible_schemas()}
    assert names == {"read_file", "search_text"}
    r = await rt.execute(
        ToolCall(id="1", name="write_file", arguments={"path": "x.py", "content": "1"})
    )
    assert not r.ok and "allowlist" in r.output and not (project / "x.py").exists()
    rt.allow_tools = set()
    rt.deny_tools = {"shell"}
    assert "shell" not in {s["function"]["name"] for s in rt.visible_schemas()}


class _Big(ScriptedProvider):
    async def complete(self, messages, tools=None):
        r = await super().complete(messages, tools)
        return r.model_copy(update={"usage": TokenUsage(input_tokens=3000, output_tokens=100)})


async def test_token_cap_stops_the_run_with_its_own_code(project, manager_factory, events):
    from trendlab.config.schema import AppConfig, PermissionMode

    from .test_agent_runtime import make_agent

    cfg = AppConfig()
    cfg.limits.max_tokens_per_run = 5000
    reads = [
        ModelResponse(
            tool_calls=[ToolCall(id=str(i), name="read_file", arguments={"path": "src/app.py"})]
        )
        for i in range(6)
    ]
    agent, _ = make_agent(
        project,
        manager_factory(),
        events,
        _Big([*reads, ModelResponse(text="x")]),
        mode=PermissionMode.UNSAFE,
        config=cfg,
    )
    result = await agent.run("what is the timeout?")
    assert result.status == "FAILED" and result.failure_code == "BUDGET_TOKENS"
    assert "token limit (5000)" in result.stop_reason


def test_verification_latency():
    t = {"edit": [10.0, 14.0], "validate": [12.0, 15.5], "verify": [18.0]}
    assert verification_latency(t, 2.0) == {
        "time_to_first_edit_s": 8.0,
        "edit_to_validation_s": 1.5,
        "edit_to_verdict_s": 4.0,
    }
    assert verification_latency({"edit": [], "validate": [], "verify": []}, 0.0) == {
        "time_to_first_edit_s": None
    }
    unvalidated = verification_latency({"edit": [5.0], "validate": [1.0], "verify": []}, 0.0)
    assert unvalidated["edit_to_validation_s"] is None
