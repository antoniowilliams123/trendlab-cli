from pathlib import Path

from rich.console import Console
from textual.widgets import RichLog

from trendlab.providers.base import ModelResponse
from trendlab.providers.scripted import ScriptedProvider
from trendlab.ui.editor import edit_in_external_editor, editor_command
from trendlab.ui.tui import PromptInput, TrendLabTUI

from .test_tui import _tl


def test_external_editor_round_trip(tmp_path: Path, monkeypatch):
    script = tmp_path / "fake_editor.sh"
    script.write_text("#!/bin/sh\nprintf 'edited by script\\nline two\\n' > \"$1\"\n")
    script.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(script))
    monkeypatch.delenv("VISUAL", raising=False)
    assert editor_command() == [str(script)]
    assert edit_in_external_editor("draft") == "edited by script\nline two\n"
    monkeypatch.setenv("EDITOR", "")
    monkeypatch.setattr("trendlab.ui.editor.shutil.which", lambda _c: None)
    assert edit_in_external_editor("x") is None


async def test_tui_multiline_prompt_and_submit(project: Path, _trendlab_home: Path):
    provider = ScriptedProvider([ModelResponse(text="got it")])
    tui = TrendLabTUI(_tl(project, provider))
    async with tui.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        box = tui.query_one("#input", PromptInput)
        box.text = "first line \\"
        await pilot.press("enter")  # trailing backslash continues instead of sending
        assert box.text == "first line\n" and not provider.calls
        box.insert("second line")
        await pilot.press("ctrl+j")  # explicit newline binding
        box.insert("third line")
        assert box.text == "first line\nsecond line\nthird line"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.02)
            if tui._run_task and tui._run_task.done():
                break
        assert (
            provider.calls
            and provider.calls[0][-1]["content"] == "first line\nsecond line\nthird line"
        )
        assert box.text == ""


async def test_tui_image_command_attaches_to_next_prompt(project: Path, _trendlab_home: Path):
    from .test_attachments import PNG

    (project / "ui.png").write_bytes(PNG)
    provider = ScriptedProvider([ModelResponse(text="seen")])
    tui = TrendLabTUI(_tl(project, provider))
    async with tui.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        box = tui.query_one("#input", PromptInput)
        box.text = "/image ui.png"
        await pilot.press("enter")
        await pilot.pause()
        text = "\n".join(str(line) for line in tui.query_one("#transcript", RichLog).lines)
        assert "attached ui.png" in text and len(tui.tl.pending_images) == 1
        box.text = "what is wrong here"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.02)
            if tui._run_task and tui._run_task.done():
                break
        sent = provider.calls[0][-1]["content"]
        assert sent[0]["type"] == "image_path" and sent[0]["path"].endswith("ui.png")
        assert sent[1]["text"] == "what is wrong here" and tui.tl.pending_images == []


async def test_repl_backslash_continuation(project: Path, _trendlab_home: Path):
    import asyncio

    from trendlab.app import TrendLabApp
    from trendlab.config.loader import load_config
    from trendlab.ui.console import ConsoleInput
    from trendlab.ui.repl import Repl

    class FakeInput(ConsoleInput):
        def __init__(self, lines):
            super().__init__()
            self._lines = list(lines)

        @property
        def interactive(self):
            return False

        async def readline(self):
            await asyncio.sleep(0)
            return self._lines.pop(0) if self._lines else None

    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    provider = ScriptedProvider([ModelResponse(text="ok")])
    console = Console(record=True, width=120)
    tl = TrendLabApp(
        project,
        cfg,
        provider=provider,
        model_ref="scripted:m",
        console=console,
        console_input=FakeInput(["write a poem \\", "about terminals", "/quit"]),
    )
    await Repl(tl, console).run()
    assert provider.calls[0][-1]["content"] == "write a poem\nabout terminals"


def test_trailing_backslash_only_continues_after_a_space():
    from trendlab.ui.prompt_rules import continues_line

    assert continues_line("first line \\")
    assert continues_line("\\")
    assert not continues_line(r"where are the pdfs in \\wsl$\Ubuntu\home\tony\results\\"[:-1])
    assert not continues_line(r"look in C:\Users\anton\Downloads\\"[:-1])
    assert not continues_line("plain text")
