import asyncio

import pytest

from trendlab.agent.runtime import AgentRuntime
from trendlab.agent.state import AgentState, AgentStateMachine, InvalidTransition
from trendlab.config.schema import PermissionMode
from trendlab.permissions.engine import PermissionEngine
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.telemetry.events import EventType
from trendlab.tools.base import ToolContext
from trendlab.tools.registry import default_registry
from trendlab.tools.runtime import ToolRuntime


def test_state_machine_rejects_invalid():
    sm = AgentStateMachine()
    sm.transition(AgentState.THINKING)
    sm.transition(AgentState.WAITING_PERMISSION)
    sm.transition(AgentState.RUNNING_TOOL)
    with pytest.raises(InvalidTransition):
        sm.transition(AgentState.IDLE)
    sm.transition(AgentState.COMPLETED)
    assert sm.terminal and sm.history[-1] == AgentState.COMPLETED


async def test_agent_loop_pauses_only_the_gated_operation(
    project, manager_factory, events, recorder
):
    mgr = manager_factory()
    engine = PermissionEngine(PermissionMode.ASK)
    ctx = ToolContext(project_root=project, session_id=mgr.session_id)
    tools = ToolRuntime(default_registry(), engine, mgr, events, ctx)
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "src/app.py"})]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="c2",
                        name="shell",
                        arguments={"command": "pip install pandas-ta", "explanation": "indicators"},
                    )
                ]
            ),
            ModelResponse(text="Installed pandas-ta and verified the import."),
        ]
    )
    agent = AgentRuntime(
        provider=provider,
        tools=tools,
        events=events,
        session_id=mgr.session_id,
        system_prompt="sys",
        max_iterations=10,
    )
    task = asyncio.create_task(agent.run("add pandas-ta"))

    for _ in range(100):
        await asyncio.sleep(0.01)
        if mgr.pending():
            break
    [req] = mgr.pending()
    assert agent.state.state == AgentState.WAITING_PERMISSION
    assert req.command == "pip install pandas-ta" and req.explanation == "indicators"
    assert len(agent.messages) >= 4  # system, user, assistant(tool), tool result, assistant(tool)
    # Approve from the "phone".
    mgr.decide(
        req.approval_id,
        "approve",
        via="web",
        by="phone",
        decision_token=req.decision_token,
        fingerprint=req.fingerprint,
    )
    reply = await task
    assert reply.startswith("Installed")
    assert agent.state.state == AgentState.COMPLETED
    transitions = [
        (e.data["old"], e.data["new"]) for e in recorder.of_type(EventType.AGENT_STATE_CHANGED)
    ]
    assert ("RUNNING_TOOL", "WAITING_PERMISSION") in transitions
    assert ("WAITING_PERMISSION", "RUNNING_TOOL") in transitions
    # The pip command actually ran (and failed or succeeded) — the observation reached the model.
    tool_msgs = [m for m in agent.messages if m["role"] == "tool" and m["tool_call_id"] == "c2"]
    assert tool_msgs and "NOT EXECUTED" not in tool_msgs[0]["content"]
    assert len(provider.calls) == 3


async def test_agent_receives_denial_as_observation(project, manager_factory, events):
    mgr = manager_factory()
    ctx = ToolContext(project_root=project, session_id=mgr.session_id)
    tools = ToolRuntime(default_registry(), PermissionEngine(PermissionMode.ASK), mgr, events, ctx)
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="c1", name="write_file", arguments={"path": "x.py", "content": "1"})
                ]
            ),
            ModelResponse(text="The edit was denied; stopping."),
        ]
    )
    agent = AgentRuntime(
        provider=provider,
        tools=tools,
        events=events,
        session_id=mgr.session_id,
        system_prompt="sys",
    )
    task = asyncio.create_task(agent.run("write x.py"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if mgr.pending():
            break
    [req] = mgr.pending()
    mgr.decide(req.approval_id, "deny", via="local", trusted=True)
    assert (await task).startswith("The edit was denied")
    assert not (project / "x.py").exists()
    assert "NOT EXECUTED: approval denied" in provider.calls[1][-1]["content"]
