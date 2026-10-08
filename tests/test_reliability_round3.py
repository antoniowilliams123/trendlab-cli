"""Attempt 3, group 1: idempotent edits, structured-output repair, JSON mode payloads,
runtime invariants, run_tests routing."""

from pathlib import Path

from trendlab.agent.invariants import check
from trendlab.config.schema import PermissionMode
from trendlab.permissions.engine import PermissionEngine
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.providers.structured_json import ask_json, json_messages
from trendlab.telemetry.events import EventType
from trendlab.tools.base import ToolContext
from trendlab.tools.registry import default_registry
from trendlab.tools.runtime import ToolRuntime

from .test_agent_runtime import make_agent


async def test_edits_are_idempotent(project: Path, manager_factory, events, recorder):
    mgr = manager_factory()
    rt = ToolRuntime(
        default_registry(),
        PermissionEngine(PermissionMode.UNSAFE),
        mgr,
        events,
        ToolContext(project_root=project, session_id=mgr.session_id),
    )
    w = ToolCall(id="1", name="write_file", arguments={"path": "src/a.py", "content": "a = 1\n"})
    assert (await rt.execute(w)).ok
    again = await rt.execute(
        ToolCall(id="2", name="write_file", arguments={"path": "src/a.py", "content": "a = 1\n"})
    )
    assert again.ok and "no change" in again.output and again.data["unchanged"]
    p = ToolCall(
        id="3",
        name="patch_file",
        arguments={"path": "src/a.py", "old_text": "a = 1", "new_text": "a = 2"},
    )
    assert (await rt.execute(p)).ok
    retry = await rt.execute(
        ToolCall(
            id="4",
            name="patch_file",
            arguments={"path": "src/a.py", "old_text": "a = 1", "new_text": "a = 2"},
        )
    )
    assert retry.ok and "already contains this change" in retry.output
    changed = recorder.of_type(EventType.FILE_CHANGED)
    assert len(changed) == 2  # only the two real changes
    missing = await rt.execute(
        ToolCall(
            id="5",
            name="patch_file",
            arguments={"path": "src/a.py", "old_text": "zzz", "new_text": "yyy"},
        )
    )
    assert not missing.ok and "patch failed" in missing.output


async def test_ask_json_repairs_once():
    seen = []

    async def call(msgs):
        seen.append(msgs)
        return "sure, here it is" if len(seen) == 1 else '{"ok": 1}'

    def parse(t):
        import json

        try:
            return json.loads(t)
        except ValueError:
            return None

    val, attempts = await ask_json(call, [{"role": "user", "content": "give json"}], parse)
    assert val == {"ok": 1} and attempts == 2
    assert seen[0][0]["_json_mode"] and "not valid JSON" in seen[1][-1]["content"]

    async def bad(msgs):
        return "never json"

    assert await ask_json(bad, [{"role": "user", "content": "x"}], parse) == (None, 2)
    assert json_messages([{"role": "user", "content": "x"}])[0]["_json_mode"] is True


def test_providers_send_json_mode():
    import asyncio

    from trendlab.providers.openai_compatible import OpenAICompatibleProvider

    prov = OpenAICompatibleProvider.__new__(OpenAICompatibleProvider)
    prov.model = "m"
    from trendlab.providers.base import ModelCapabilities

    prov._caps = ModelCapabilities()
    pl = prov._payload(json_messages([{"role": "user", "content": "x"}]), None, stream=False)
    assert (
        pl["response_format"] == {"type": "json_object"} and "_json_mode" not in pl["messages"][0]
    )
    assert "response_format" not in prov._payload(
        [{"role": "user", "content": "x"}], None, stream=False
    )

    from trendlab.providers.ollama_provider import OllamaProvider

    ol = OllamaProvider.__new__(OllamaProvider)
    ol.model = "m"
    ol._keep_alive = "5m"
    ol._caps = ModelCapabilities()

    async def ctx():
        return 8192

    ol.num_ctx = ctx
    ol._messages = lambda ms: ms
    out = asyncio.run(ol._payload(json_messages([{"role": "user", "content": "x"}]), None, False))
    assert out["format"] == "json"


async def test_invariants_flag_escapes_and_stop_the_run(
    project: Path, manager_factory, events, recorder
):
    agent, tools = make_agent(
        project,
        manager_factory(),
        events,
        ScriptedProvider([ModelResponse(text="hi")]),
        mode=PermissionMode.UNSAFE,
    )
    assert check(agent) == []
    tools.changed_files["../outside.py"] = [""]
    problems = check(agent)
    assert problems and problems[0][1] is True and "outside the project" in problems[0][0]
    tools.changed_files.clear()
    # an escaped path mid-run stops the run with a readable reason
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="r", name="read_file", arguments={"path": "README.md"})]
            ),
            ModelResponse(text="done"),
        ]
    )
    agent2, tools2 = make_agent(
        project, manager_factory(), events, provider, mode=PermissionMode.UNSAFE
    )
    tools2.changed_files["/etc/passwd"] = [""]
    result = await agent2.run("look")
    assert result.status == "FAILED" and result.stop_reason.startswith("invariant violated")
    assert recorder.of_type(EventType.INVARIANT_VIOLATED)[0].data["fatal"] is True


def test_run_tests_hidden_without_a_test_command(project: Path, manager_factory, events):
    mgr = manager_factory()
    rt = ToolRuntime(
        default_registry(),
        PermissionEngine(PermissionMode.UNSAFE),
        mgr,
        events,
        ToolContext(project_root=project, session_id=mgr.session_id, validation_commands={}),
    )
    assert "run_tests" not in {s["function"]["name"] for s in rt.visible_schemas()}
    rt.ctx.validation_commands = {"test": "pytest -q"}
    assert "run_tests" in {s["function"]["name"] for s in rt.visible_schemas()}
