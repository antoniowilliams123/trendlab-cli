import asyncio
from pathlib import Path

from textual.widgets import Input, RichLog

from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.telemetry.events import EventType
from trendlab.ui.tui import TrendLabTUI

from .test_agent_runtime import make_agent
from .test_tui import _tl


async def test_steering_message_is_delivered_before_next_model_call(
    project, manager_factory, events, recorder
):
    mgr = manager_factory()
    first_started = asyncio.Event()
    release = asyncio.Event()

    class Gated(ScriptedProvider):
        async def complete(self, messages, tools=None):
            if not self.calls:
                first_started.set()
                await release.wait()  # hold the first call so steering arrives mid-run
            return await super().complete(messages, tools)

    provider = Gated(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="1", name="read_file", arguments={"path": "src/app.py"})]
            ),
            ModelResponse(text="done after steering"),
        ]
    )
    agent, _ = make_agent(project, mgr, events, provider)
    task = asyncio.create_task(agent.run("start"))
    await first_started.wait()
    agent.steer("actually, focus on the README instead")
    release.set()
    result = await task
    assert result.status == "COMPLETED"
    second_call = provider.calls[1]
    user_turns = [m["content"] for m in second_call if m["role"] == "user"]
    assert user_turns[-1] == "actually, focus on the README instead"
    assert second_call[-1]["role"] == "user"  # steering lands right before the model call
    assert recorder.of_type(EventType.STEERED)[0].data["text"].startswith("actually")


async def test_interrupt_keeps_conversation_and_allows_continuation(
    project, manager_factory, events
):
    mgr = manager_factory()
    started = asyncio.Event()

    class HangingProvider(ScriptedProvider):
        async def complete(self, messages, tools=None):
            self.calls.append(list(messages))
            started.set()
            await asyncio.sleep(30)
            return ModelResponse(text="never")

    provider = HangingProvider([])
    agent, _ = make_agent(project, mgr, events, provider)
    task = asyncio.create_task(agent.run("long task"))
    await started.wait()
    agent.cancel()
    task.cancel()
    result = await task
    assert result.status == "CANCELED"
    assert agent.messages[-1] == {"role": "user", "content": "long task"}  # history intact
    # The same runtime continues with the conversation.
    provider2 = ScriptedProvider([ModelResponse(text="continued")])
    agent.gateway.register("scripted:m", provider2)
    result = await agent.run("keep going")
    assert result.status == "COMPLETED" and result.text == "continued"
    assert [m["content"] for m in provider2.calls[0] if m["role"] == "user"] == [
        "long task",
        "keep going",
    ]


async def test_tui_typing_during_run_steers_and_escape_interrupts(
    project: Path, _trendlab_home: Path
):
    first_started = asyncio.Event()
    release = asyncio.Event()

    class Slow(ScriptedProvider):
        async def complete(self, messages, tools=None):
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                first_started.set()
                await release.wait()
                return ModelResponse(
                    tool_calls=[
                        ToolCall(id="1", name="read_file", arguments={"path": "src/app.py"})
                    ]
                )
            if len(self.calls) == 2:
                return ModelResponse(text="finished with steering")
            await asyncio.sleep(30)
            return ModelResponse(text="never")

    provider = Slow([])
    tui = TrendLabTUI(_tl(project, provider))
    async with tui.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        inp = tui.query_one("#input", Input)
        inp.value = "do the task"
        await pilot.press("enter")
        await first_started.wait()
        inp.value = "also update the docs"
        await pilot.press("enter")
        await pilot.pause()
        text = "\n".join(str(line) for line in tui.query_one("#transcript", RichLog).lines)
        assert "steering" in text and "also update the docs" in text
        release.set()
        for _ in range(200):
            await pilot.pause(0.02)
            if tui._run_task and tui._run_task.done():
                break
        assert [m["content"] for m in provider.calls[1] if m["role"] == "user"][
            -1
        ] == "also update the docs"
        # Third run hangs; Esc interrupts it and the UI stays usable.
        inp.value = "another task"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.02)
            if len(provider.calls) >= 3:
                break
        await pilot.press("escape")
        for _ in range(100):
            await pilot.pause(0.02)
            if tui._run_task is None or tui._run_task.done():
                break
        await pilot.pause()
        text = "\n".join(str(line) for line in tui.query_one("#transcript", RichLog).lines)
        assert "Interrupted" in text
        assert tui.tl.agent.state.state.value == "CANCELED"
        assert tui.query_one("#input", Input).has_focus or True  # input remains available
