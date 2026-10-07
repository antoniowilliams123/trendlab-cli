import asyncio

import pytest

from trendlab.agent.runtime import AgentRuntime
from trendlab.agent.state import AgentState, AgentStateMachine, InvalidTransition
from trendlab.agent.tasks import Plan
from trendlab.config.schema import AppConfig, ContextConfig, LimitsConfig, PermissionMode
from trendlab.context.manager import ContextManager
from trendlab.permissions.engine import PermissionEngine
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.gateway import ModelGateway
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


def make_agent(project, mgr, events, provider, *, mode=PermissionMode.ASK, config=None, **kw):
    config = config or AppConfig()
    engine = PermissionEngine(mode)
    ctx = ToolContext(
        project_root=project,
        session_id=mgr.session_id,
        validation_commands=kw.pop("validation", {"test": "python3 -c 'print(1)'"}),
    )
    tools = ToolRuntime(default_registry(), engine, mgr, events, ctx)
    gw = ModelGateway(config, events, mgr.session_id, provider_factory=lambda ref: provider)
    context = ContextManager(ContextConfig(), events, mgr.session_id, system_prompt="sys")
    agent = AgentRuntime(
        gateway=gw,
        model_ref="scripted:m",
        tools=tools,
        context=context,
        events=events,
        session_id=mgr.session_id,
        limits=config.limits,
        plan=Plan(),
        stream=False,
        **kw,
    )
    return agent, tools


async def wait_pending(mgr):
    for _ in range(200):
        await asyncio.sleep(0.01)
        if mgr.pending():
            return mgr.pending()[0]
    raise AssertionError("no pending approval")


async def test_agent_loop_pauses_only_the_gated_operation(
    project, manager_factory, events, recorder
):
    mgr = manager_factory()
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
    agent, _ = make_agent(project, mgr, events, provider)
    task = asyncio.create_task(agent.run("add pandas-ta"))
    req = await wait_pending(mgr)
    assert agent.state.state == AgentState.WAITING_PERMISSION
    assert req.command == "pip install pandas-ta" and req.explanation == "indicators"
    mgr.decide(
        req.approval_id,
        "approve",
        via="web",
        by="phone",
        decision_token=req.decision_token,
        fingerprint=req.fingerprint,
    )
    result = await task
    assert result.status == "COMPLETED" and result.text.startswith("Installed")
    assert "✓ Completed" in result.report and "Cost" in result.report
    transitions = [
        (e.data["old"], e.data["new"]) for e in recorder.of_type(EventType.AGENT_STATE_CHANGED)
    ]
    assert ("RUNNING_TOOL", "WAITING_PERMISSION") in transitions
    assert ("WAITING_PERMISSION", "RUNNING_TOOL") in transitions
    tool_msgs = [m for m in agent.messages if m["role"] == "tool" and m["tool_call_id"] == "c2"]
    assert tool_msgs and "NOT EXECUTED" not in tool_msgs[0]["content"]
    assert len(provider.calls) == 3 and recorder.of_type(EventType.RUN_COMPLETED)


async def test_agent_receives_denial_as_observation(project, manager_factory, events):
    mgr = manager_factory()
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
    agent, _ = make_agent(project, mgr, events, provider)
    task = asyncio.create_task(agent.run("write x.py"))
    req = await wait_pending(mgr)
    mgr.decide(req.approval_id, "deny", via="local", trusted=True)
    result = await task
    assert result.status == "COMPLETED" and result.text.startswith("The edit was denied")
    assert not (project / "x.py").exists()
    assert "NOT EXECUTED: approval denied" in provider.calls[1][-1]["content"]


async def test_evaluator_nudges_unvalidated_mutation_then_accepts(
    project, manager_factory, events, recorder
):
    mgr = manager_factory()
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="c1", name="write_file", arguments={"path": "new.py", "content": "x=1\n"}
                    )
                ]
            ),
            ModelResponse(text="Done."),  # premature: files changed, nothing validated
            ModelResponse(
                tool_calls=[ToolCall(id="c2", name="run_tests", arguments={"kind": "test"})]
            ),
            ModelResponse(text="Done, tests pass."),
        ]
    )
    agent, tools = make_agent(project, mgr, events, provider, mode=PermissionMode.AUTO_EDIT)
    result = await agent.run("create new.py")
    assert result.status == "COMPLETED" and result.text == "Done, tests pass."
    nudges = [
        m for m in agent.messages if m["role"] == "user" and "Before finishing" in m["content"]
    ]
    assert len(nudges) == 1 and "no validation" in nudges[0]["content"]
    assert result.changed_files == ["new.py"] and result.validation_runs[0]["ok"]
    assert "✓ python3" in result.report and "- new.py" in result.report
    assert recorder.of_type(EventType.RECOVERY)[0].data["failure"] == "UNVERIFIED_COMPLETION"


async def test_evaluator_gives_up_after_max_nudges(project, manager_factory, events):
    mgr = manager_factory()
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="c1", name="write_file", arguments={"path": "n.py", "content": "1"})
                ]
            ),
            ModelResponse(text="Done."),
            ModelResponse(text="Done."),
            ModelResponse(text="Really done."),
        ]
    )
    agent, _ = make_agent(project, mgr, events, provider, mode=PermissionMode.AUTO_EDIT)
    result = await agent.run("x")
    assert (
        result.status == "COMPLETED" and result.text == "Really done." and len(provider.calls) == 4
    )
    assert "- none run" in result.report


async def test_plan_tool_and_open_task_nudge(project, manager_factory, events, recorder):
    mgr = manager_factory()
    from trendlab.tools.task_tool import TaskTool

    plan = Plan()
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="task",
                        arguments={"action": "plan", "titles": ["Inspect", "Fix", "Test"]},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="c2",
                        name="task",
                        arguments={
                            "action": "complete",
                            "task_id": "T-1",
                            "evidence": "read app.py",
                        },
                    )
                ]
            ),
            ModelResponse(text="All done."),  # T-2, T-3 still open → nudge
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="c3", name="task", arguments={"action": "complete", "task_id": "T-2"}
                    )
                ]
            ),  # missing evidence → error
            ModelResponse(text="Fix cannot be completed: blocked by missing requirements."),
        ]
    )
    agent, tools = make_agent(project, mgr, events, provider)
    tools.registry.register(TaskTool(plan, events))
    agent.plan = plan
    result = await agent.run("do it")
    assert result.status == "COMPLETED"
    assert [t.status.value for t in plan.tasks] == ["completed", "active", "pending"]
    assert plan.tasks[0].evidence == ["read app.py"]
    assert "requires evidence" in provider.calls[4][-1]["content"]
    assert "plan tasks are still open" in provider.calls[3][-1]["content"]
    # The system prompt stays byte-stable within a run (prompt-cache friendly): the plan the
    # model created mid-run is not re-rendered into it until the next run starts.
    assert all("Current plan:" not in call[0]["content"] for call in provider.calls[:5])
    provider._responses.append(ModelResponse(text="second run; nothing to do"))
    await agent.run("status?")
    assert (
        "Current plan:" in provider.calls[-1][0]["content"]
        and "[✓] T-1 Inspect" in provider.calls[-1][0]["content"]
    )
    assert recorder.of_type(EventType.PLAN_UPDATED)


async def test_loop_detection_feedback_then_stop(project, manager_factory, events, recorder):
    mgr = manager_factory()
    same = ToolCall(id="c", name="read_file", arguments={"path": "src/app.py"})
    provider = ScriptedProvider([ModelResponse(tool_calls=[same]) for _ in range(12)])
    agent, _ = make_agent(project, mgr, events, provider)
    result = await agent.run("loop")
    assert result.status == "FAILED" and result.stop_reason.startswith("NO_PROGRESS")
    loops = recorder.of_type(EventType.LOOP_DETECTED)
    assert [e.data["action"] for e in loops] == ["feedback", "stop"]
    fed = [
        m for m in agent.messages if m["role"] == "tool" and "NO_PROGRESS detected" in m["content"]
    ]
    assert len(fed) == 1 and len(provider.calls) == 6


async def test_loop_escalates_to_stronger_model(project, manager_factory, events, recorder):
    mgr = manager_factory()
    same = ToolCall(id="c", name="read_file", arguments={"path": "src/app.py"})
    weak = ScriptedProvider([ModelResponse(tool_calls=[same]) for _ in range(6)])
    strong = ScriptedProvider([ModelResponse(text="Strong model finished.")])
    config = AppConfig()
    engine = PermissionEngine(PermissionMode.ASK)
    ctx = ToolContext(project_root=project, session_id=mgr.session_id)
    tools = ToolRuntime(default_registry(), engine, mgr, events, ctx)
    gw = ModelGateway(
        config,
        events,
        mgr.session_id,
        provider_factory={"weak:m": weak, "strong:m": strong}.__getitem__,
    )
    context = ContextManager(ContextConfig(), events, mgr.session_id, system_prompt="sys")
    agent = AgentRuntime(
        gateway=gw,
        model_ref="weak:m",
        tools=tools,
        context=context,
        events=events,
        session_id=mgr.session_id,
        escalation_model="strong:m",
        stream=False,
    )
    result = await agent.run("loop")
    assert result.status == "COMPLETED" and result.text == "Strong model finished."
    # Escalation lasts for the rest of the run; the default model is restored afterwards.
    assert agent.model_ref == "weak:m" and agent.escalation_model == "strong:m"
    esc = [e for e in recorder.of_type(EventType.RECOVERY) if e.data.get("action") == "escalate"]
    assert esc and esc[0].data["to_model"] == "strong:m"


async def test_limits_stop_the_run(project, manager_factory, events, recorder):
    mgr = manager_factory()
    same = ToolCall(id="c", name="list_directory", arguments={"path": "."})
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id=str(i),
                        name="list_directory",
                        arguments={"path": "." if i % 2 else "src"},
                    )
                ]
            )
            for i in range(20)
        ]
    )
    cfg = AppConfig(limits=LimitsConfig(max_iterations=3))
    agent, _ = make_agent(project, mgr, events, provider, config=cfg)
    result = await agent.run("x")
    assert (
        result.status == "FAILED"
        and "max_iterations" in result.stop_reason
        and result.iterations == 3
    )
    assert recorder.of_type(EventType.RUN_FAILED)

    from trendlab.config.schema import ModelPricing
    from trendlab.providers.base import TokenUsage

    cfg2 = AppConfig(
        limits=LimitsConfig(max_cost_usd=0.001),
        pricing={"scripted:m": ModelPricing(output_per_million=1000.0)},
    )
    provider2 = ScriptedProvider(
        [ModelResponse(tool_calls=[same], usage=TokenUsage(output_tokens=5)) for _ in range(5)]
    )
    agent2, _ = make_agent(project, mgr, events, provider2, config=cfg2)
    result = await agent2.run("x")
    assert result.status == "FAILED" and "cost limit" in result.stop_reason
    assert recorder.of_type(EventType.BUDGET_EXCEEDED) and result.cost_usd >= 0.001


async def test_cancellation_mid_run(project, manager_factory, events, recorder):
    mgr = manager_factory()
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="c1", name="shell", arguments={"command": "pip install x"})]
            ),
            ModelResponse(text="never"),
        ]
    )
    agent, _ = make_agent(project, mgr, events, provider)
    task = asyncio.create_task(agent.run("install"))
    req = await wait_pending(mgr)
    agent.cancel()
    task.cancel()
    result = await task
    assert result.status == "CANCELED"
    assert mgr.pending() == [] and mgr.store.get_approval(req.approval_id)["status"] == "canceled"
    assert recorder.of_type(EventType.RUN_CANCELED)


async def test_provider_failure_is_reported_not_raised(project, manager_factory, events):
    from trendlab.providers.base import ProviderAuthenticationError

    mgr = manager_factory()
    provider = ScriptedProvider([ProviderAuthenticationError("bad key")])
    agent, _ = make_agent(project, mgr, events, provider)
    result = await agent.run("x")
    assert (
        result.status == "FAILED"
        and "AUTH_FAILURE" in result.stop_reason
        and "✗ Failed" in result.report
    )


async def test_context_overflow_triggers_compaction_then_retry(
    project, manager_factory, events, recorder
):
    from trendlab.providers.base import ProviderContextOverflowError

    mgr = manager_factory()
    provider = ScriptedProvider(
        [ProviderContextOverflowError("too long"), ModelResponse(text="ok after compaction")]
    )
    agent, _ = make_agent(project, mgr, events, provider)
    agent.context.messages = [
        {"role": "user", "content": "old " * 50},
        {"role": "assistant", "content": "a"},
    ] * 4
    result = await agent.run("continue")
    assert result.status == "COMPLETED" and result.text == "ok after compaction"
    assert recorder.of_type(EventType.CONTEXT_COMPACTED)
    assert agent.context.summary is not None and agent.context.summary.source == "deterministic"
