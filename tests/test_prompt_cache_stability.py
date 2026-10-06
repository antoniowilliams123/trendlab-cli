"""The system prompt must stay byte-identical within a run so provider prompt caches hit."""

from pathlib import Path

from trendlab.agent.tasks import Plan
from trendlab.config.schema import PermissionMode
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.tools.task_tool import TaskTool

from .test_agent_runtime import make_agent


class RecordingProvider(ScriptedProvider):
    def __init__(self, responses):
        super().__init__(responses)
        self.systems: list[str] = []

    async def complete(self, messages, tools=None):
        self.systems.append(messages[0]["content"])
        return await super().complete(messages, tools)


async def test_system_prompt_stable_within_run_and_refreshed_next_run(
    project: Path, manager_factory, events
):
    mgr = manager_factory()
    provider = RecordingProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="p",
                        name="task",
                        arguments={"action": "plan", "titles": ["Inspect"]},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[ToolCall(id="r", name="read_file", arguments={"path": "src/app.py"})]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="c",
                        name="task",
                        arguments={"action": "complete", "task_id": "T-1", "evidence": "read it"},
                    )
                ]
            ),
            ModelResponse(text="nothing to change; no validation needed"),
            ModelResponse(text="second run; nothing changed"),
        ]
    )
    agent, tools = make_agent(project, mgr, events, provider, mode=PermissionMode.UNSAFE)
    plan = Plan()
    tools.registry.register(TaskTool(plan, events))
    agent.plan = plan
    result = await agent.run("look around")
    assert result.status == "COMPLETED"
    first_run = list(provider.systems)
    n1 = len(first_run)
    assert n1 >= 4
    # Plan was created and updated during the run, yet the cached prefix never changed.
    assert len(set(first_run)) == 1 and "Current plan" not in first_run[0]
    assert agent.plan.tasks
    await agent.run("again")
    assert "Current plan" in provider.systems[n1] and "Inspect" in provider.systems[n1]
