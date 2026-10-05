"""Textual TUI smoke tests via the headless pilot."""

from pathlib import Path

from rich.console import Console
from textual.widgets import RichLog, Static

from trendlab.app import TrendLabApp
from trendlab.config.loader import load_config
from trendlab.config.schema import PermissionMode, RemoteApprovalConfig
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.ui.tui import ApprovalModal, PromptInput, TrendLabTUI


def _tl(project: Path, provider, mode=PermissionMode.ASK):
    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    return TrendLabApp(
        project,
        cfg,
        provider=provider,
        model_ref="scripted:m",
        permission_mode=mode,
        console=Console(record=True, width=100, force_terminal=False),
    )


async def test_tui_runs_a_prompt_and_slash_commands(project: Path, _trendlab_home: Path):
    tl = _tl(project, ScriptedProvider([ModelResponse(text="hello from the tui")]))
    tui = TrendLabTUI(tl)
    async with tui.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        header = tui.query_one("#header", Static).content
        assert "scripted:m" in str(header) and "ASK" in str(header)
        inp = tui.query_one("#input", PromptInput)
        inp.text = "/status"
        await pilot.press("enter")
        await pilot.pause()
        log = tui.query_one("#transcript", RichLog)
        text = "\n".join(str(line) for line in log.lines)
        assert "Mode" in text or "Model" in text
        inp.text = "say hi"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.02)
            if tui._run_task and tui._run_task.done():
                break
        await pilot.pause()
        text = "\n".join(str(line) for line in log.lines)
        assert "hello from the tui" in text and "Completed" in text


async def test_tui_approval_modal_approves_and_continues(project: Path, _trendlab_home: Path):
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="1", name="write_file", arguments={"path": "t.py", "content": "ok\n"}
                    )
                ]
            ),
            ModelResponse(text="written; validation not possible here"),
        ]
    )
    tl = _tl(project, provider)
    tui = TrendLabTUI(tl)
    async with tui.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        tui.query_one("#input", PromptInput).text = "write t.py"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.02)
            if isinstance(tui.screen, ApprovalModal):
                break
        assert isinstance(tui.screen, ApprovalModal)
        assert tui.screen.request.summary.startswith("Create t.py")
        await pilot.press("y")
        for _ in range(100):
            await pilot.pause(0.02)
            if tui._run_task and tui._run_task.done():
                break
        await pilot.pause()
        assert (project / "t.py").read_text() == "ok\n"
        text = "\n".join(str(line) for line in tui.query_one("#transcript", RichLog).lines)
        assert "Approved locally" in text


async def test_tui_question_modal_answers(project: Path, _trendlab_home: Path):
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="1",
                        name="ask_user",
                        arguments={"question": "Which?", "options": ["a", "b"]},
                    )
                ]
            ),
            ModelResponse(text="Chosen; done."),
        ]
    )
    tl = _tl(project, provider)
    tui = TrendLabTUI(tl)
    async with tui.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        tui.query_one("#input", PromptInput).text = "ask me"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.02)
            if isinstance(tui.screen, ApprovalModal):
                break
        assert tui.screen.request.kind == "question"
        await pilot.click("#opt1")
        for _ in range(100):
            await pilot.pause(0.02)
            if tui._run_task and tui._run_task.done():
                break
        assert "USER ANSWER: b" in provider.calls[1][-1]["content"]


async def test_tui_theme_is_black_and_neon(project: Path, _trendlab_home: Path):
    tl = _tl(project, ScriptedProvider([]))
    tui = TrendLabTUI(tl)
    async with tui.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        bg = tui.screen.styles.background
        assert (bg.r, bg.g, bg.b) == (0, 0, 0)
        log = tui.query_one("#transcript", RichLog)
        fg = log.styles.color
        assert (fg.r, fg.g, fg.b) == (0x9D, 0xFF, 0x8A)  # soft neon body text
        header = tui.query_one("#header", Static)
        assert (header.styles.color.r, header.styles.color.g, header.styles.color.b) == (
            0x39,
            0xFF,
            0x14,
        )
        status = tui.query_one("#status", Static)
        assert "ctx" in str(status.content) and "cost" in str(status.content)
        svg = tui.export_screenshot()
        (_trendlab_home / "tui.svg").write_text(svg)
        assert "#000000" in svg.lower() and "#39ff14" in svg.lower()
