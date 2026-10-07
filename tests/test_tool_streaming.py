"""Streaming tool output (spec §92.3): live tail while a command runs, nothing persisted."""

from pathlib import Path

from rich.console import Console
from textual.widgets import Static

from trendlab.app import TrendLabApp
from trendlab.config.loader import load_config
from trendlab.config.schema import PermissionMode, RemoteApprovalConfig
from trendlab.permissions.engine import PermissionEngine
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.telemetry.events import TRANSIENT_EVENTS, EventType
from trendlab.tools.base import ToolContext
from trendlab.tools.registry import default_registry
from trendlab.tools.runtime import ToolRuntime
from trendlab.ui.tui import PromptInput, TrendLabTUI

SLOW = "for i in 1 2 3 4 5; do echo line $i; sleep 0.15; done; echo err >&2; exit 0"


async def test_shell_reports_live_tail_and_final_output(
    project: Path, manager_factory, events, recorder
):
    mgr = manager_factory()
    rt = ToolRuntime(
        default_registry(),
        PermissionEngine(PermissionMode.UNSAFE),
        mgr,
        events,
        ToolContext(project_root=project, session_id=mgr.session_id),
    )
    res = await rt.execute(ToolCall(id="1", name="shell", arguments={"command": SLOW}))
    assert res.ok and "line 5" in res.output and "[stderr]" in res.output and "err" in res.output
    outs = recorder.of_type(EventType.TOOL_OUTPUT)
    assert len(outs) >= 2  # throttled to ~0.3 s, so a 0.75 s command yields a few reports
    assert outs[0].data["tool"] == "shell" and "line 1" in outs[0].data["tail"]
    assert "line 5" in outs[-1].data["tail"] and outs[-1].data["elapsed_s"] >= 0.5
    assert rt.ctx.progress is None  # cleared after the call
    assert "tool.output" in TRANSIENT_EVENTS


async def test_transient_events_are_not_persisted(project: Path, _trendlab_home: Path):
    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    provider = ScriptedProvider(
        [
            ModelResponse(tool_calls=[ToolCall(id="1", name="shell", arguments={"command": SLOW})]),
            ModelResponse(text="ran it; nothing changed"),
        ]
    )
    tl = TrendLabApp(
        project,
        cfg,
        provider=provider,
        model_ref="scripted:m",
        permission_mode=PermissionMode.UNSAFE,
        console=Console(record=True, width=100, force_terminal=False),
    )
    await tl.start(interactive=False)
    try:
        seen = []
        tl.events.subscribe(lambda e: seen.append(e))
        await tl.run_prompt("run the slow thing")
        assert any(e.type == EventType.TOOL_OUTPUT for e in seen)
        types = {e["type"] for e in tl.store.events(tl.session_id)}
        assert "tool.output" not in types and "tool.completed" in types
        log = (tl.data_dir / "logs" / "events.jsonl").read_text()
        assert '"tool.output"' not in log
    finally:
        await tl.stop()


async def test_tui_shows_live_tail_then_clears(project: Path, _trendlab_home: Path):
    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    provider = ScriptedProvider(
        [
            ModelResponse(tool_calls=[ToolCall(id="1", name="shell", arguments={"command": SLOW})]),
            ModelResponse(text="done; nothing changed"),
        ]
    )
    tui = TrendLabTUI(
        TrendLabApp(
            project,
            cfg,
            provider=provider,
            model_ref="scripted:m",
            permission_mode=PermissionMode.UNSAFE,
            console=Console(record=True, width=100, force_terminal=False),
        )
    )
    async with tui.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        tui.query_one("#input", PromptInput).text = "run it"
        await pilot.press("enter")
        saw_tail = False
        for _ in range(300):
            await pilot.pause(0.02)
            pane = tui.query_one("#stream", Static)
            if (
                pane.has_class("visible")
                and "● shell" in str(pane.content)
                and "line" in str(pane.content)
            ):
                saw_tail = True
            if tui._run_task and tui._run_task.done():
                break
        assert saw_tail
        await pilot.pause()
        pane = tui.query_one("#stream", Static)
        assert not pane.has_class("visible") and tui._tool_tail is None
