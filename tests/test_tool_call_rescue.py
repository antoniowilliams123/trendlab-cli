"""A tool call the model wrote as text is executed instead of accepted as the answer."""

from pathlib import Path

from trendlab.agent.rescue import rescue_text_tool_calls
from trendlab.config.schema import PermissionMode
from trendlab.providers.base import ModelResponse
from trendlab.providers.scripted import ScriptedProvider
from trendlab.telemetry.events import EventType

from .test_agent_runtime import make_agent

KNOWN = ["list_directory", "read_file", "write_file"]


def test_rescue_shapes():
    r = rescue_text_tool_calls('{"name": "list_directory", "arguments": {"path": "./"}}', KNOWN)
    assert [(c.name, c.arguments) for c in r] == [("list_directory", {"path": "./"})]
    fenced = 'Sure.\n```json\n{"action": "read_file", "arguments": {"path": "a.py"}}\n```'
    assert rescue_text_tool_calls(fenced, KNOWN)[0].name == "read_file"
    openai_shape = '{"function": {"name": "read_file", "arguments": "{\\"path\\": \\"b.py\\"}"}}'
    assert rescue_text_tool_calls(openai_shape, KNOWN)[0].arguments == {"path": "b.py"}
    many = '{"tool_calls": [{"tool": "read_file", "parameters": {"path": "x"}}, {"name": "list_directory", "input": {"path": "."}}]}'
    assert [c.name for c in rescue_text_tool_calls(many, KNOWN)] == ["read_file", "list_directory"]
    as_list = '[{"name": "read_file", "args": {"path": "x"}}]'
    assert len(rescue_text_tool_calls(as_list, KNOWN)) == 1
    # Not rescued: unknown tool, bad args, real prose with an example, plain answers.
    assert rescue_text_tool_calls('{"name": "rm_rf", "arguments": {}}', KNOWN) == []
    assert rescue_text_tool_calls('{"name": "read_file", "arguments": "not json"}', KNOWN) == []
    prose = (
        "Here is how the tool protocol works. " * 10
        + '{"name": "read_file", "arguments": {"path": "x"}}'
    )
    assert rescue_text_tool_calls(prose, KNOWN) == []
    assert rescue_text_tool_calls("The directory has 3 files: {a, b, c}.", KNOWN) == []
    assert rescue_text_tool_calls("", KNOWN) == [] and rescue_text_tool_calls(None, KNOWN) == []


def test_rescue_announced_call_after_long_prose():
    from trendlab.agent.rescue import prose_outside_calls

    text = (
        "To identify the 6 most recently modified files I would need modification dates. "
        "The listing above has none, so I need to use list_directory with details. "
        "Here is the command to run:\n\n```json\n"
        '{"name": "list_directory", "arguments": {"path": "/home/tony/x"}}\n```'
    )
    calls = rescue_text_tool_calls(text, KNOWN)
    assert [c.name for c in calls] == ["list_directory"]
    assert prose_outside_calls(text).startswith("To identify") and "```" not in prose_outside_calls(
        text
    )
    # Same prose but the JSON is quoted mid-sentence, with more text after it: an explanation.
    quoted = (
        text
        + "\n\nThat is how the protocol works in general; let me know if you want me to run it."
    )
    assert rescue_text_tool_calls(quoted, KNOWN) == []


async def test_runtime_executes_rescued_call(project: Path, manager_factory, events, recorder):
    mgr = manager_factory()
    provider = ScriptedProvider(
        [
            ModelResponse(text='{"name": "list_directory", "arguments": {"path": "src"}}'),
            ModelResponse(text="src contains app.py; nothing to change"),
        ]
    )
    agent, _tools = make_agent(project, mgr, events, provider, mode=PermissionMode.UNSAFE)
    result = await agent.run("what is in src?")
    assert result.status == "COMPLETED" and "app.py" in result.text
    rescues = [
        e
        for e in recorder.of_type(EventType.RECOVERY)
        if e.data.get("failure") == "TOOL_CALL_AS_TEXT"
    ]
    assert rescues and rescues[0].data["tools"] == ["list_directory"]
    tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
    assert tool_msgs and "app.py" in tool_msgs[0]["content"]
    assert not any(
        m["role"] == "assistant" and (m.get("content") or "").startswith("{")
        for m in agent.messages
    )
