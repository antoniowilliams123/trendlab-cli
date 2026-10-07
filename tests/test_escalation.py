"""Quality-triggered escalation (spec §92.2): two evaluator rejections hand the run to the
stronger model; it is restored when the run ends; loop escalation uses the same path."""

from pathlib import Path

from trendlab.agent.evaluator import CompletionEvaluator
from trendlab.config.schema import AppConfig, PermissionMode
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.telemetry.events import EventType

from .test_agent_runtime import make_agent


class TwoModels(ScriptedProvider):
    """One fake provider serving two model refs so we can see which model each call used."""

    def __init__(self, weak, strong):
        super().__init__([])
        self.weak, self.strong = list(weak), list(strong)
        self.used: list[str] = []
        self.current = "weak"

    async def complete(self, messages, tools=None):
        self.calls.append(list(messages))
        self.used.append(self.current)
        queue = self.weak if self.current == "weak" else self.strong
        return queue.pop(0) if queue else ModelResponse(text="(exhausted)")


async def test_two_rejections_escalate_then_restore(
    project: Path, manager_factory, events, recorder
):
    mgr = manager_factory()
    write = ModelResponse(
        tool_calls=[
            ToolCall(
                id="w", name="write_file", arguments={"path": "src/x.py", "content": "x = 1\n"}
            )
        ]
    )
    provider = TwoModels(
        weak=[
            write,
            ModelResponse(text="done"),
            ModelResponse(text="really done"),
        ],  # never validates
        strong=[
            ModelResponse(tool_calls=[ToolCall(id="t", name="run_tests", arguments={})]),
            ModelResponse(text="validated and done"),
        ],
    )
    cfg = AppConfig()
    agent, _tools = make_agent(
        project, mgr, events, provider, mode=PermissionMode.UNSAFE, config=cfg
    )
    agent.escalation_model = "strong:m"
    # route "strong:m" to the same fake provider, flipping its persona when the agent switches
    gw = agent.gateway
    orig = gw.provider

    def provider_for(ref):
        provider.current = "strong" if ref == "strong:m" else "weak"
        return orig("scripted:m") if ref != "strong:m" else provider

    gw.provider = provider_for
    agent.evaluator = CompletionEvaluator(max_nudges=2)
    result = await agent.run("add x.py")
    assert result.status == "COMPLETED" and "validated" in result.text
    esc = [e for e in recorder.of_type(EventType.RECOVERY) if e.data.get("action") == "escalate"]
    assert (
        esc
        and esc[0].data["failure"] == "UNVERIFIED_COMPLETION"
        and esc[0].data["to_model"] == "strong:m"
    )
    assert provider.used[:3] == ["weak", "weak", "weak"] and "strong" in provider.used[3:]
    restored = [
        e for e in recorder.of_type(EventType.RECOVERY) if e.data.get("action") == "restored"
    ]
    assert restored and agent.model_ref == "scripted:m"  # back on the default for the next run
    assert agent.escalation_model == "strong:m"  # still available next run


async def test_no_escalation_model_keeps_old_behaviour(
    project: Path, manager_factory, events, recorder
):
    mgr = manager_factory()
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="w", name="write_file", arguments={"path": "src/y.py", "content": "y\n"}
                    )
                ]
            ),
            ModelResponse(text="done"),
            ModelResponse(text="done again"),
            ModelResponse(text="done, no validation possible"),
        ]
    )
    agent, _ = make_agent(project, mgr, events, provider, mode=PermissionMode.UNSAFE)
    agent.escalation_model = None
    result = await agent.run("add y.py")
    assert result.status == "COMPLETED"
    assert not [
        e for e in recorder.of_type(EventType.RECOVERY) if e.data.get("action") == "escalate"
    ]
