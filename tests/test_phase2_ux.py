"""Prompt history, slash-command menu, compact layout, background update check (spec §91.2)."""

from pathlib import Path

from rich.console import Console
from textual.widgets import Static

from trendlab.app import TrendLabApp
from trendlab.config.loader import load_config
from trendlab.config.schema import PermissionMode, RemoteApprovalConfig
from trendlab.providers.base import ModelResponse
from trendlab.providers.scripted import ScriptedProvider
from trendlab.ui.commands import command_catalog
from trendlab.ui.history import PromptHistory
from trendlab.ui.tui import CommandPicker, PromptInput, TrendLabTUI


def _tl(project: Path, provider=None):
    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    return TrendLabTUI(
        TrendLabApp(
            project,
            cfg,
            provider=provider or ScriptedProvider([ModelResponse(text="ok")] * 5),
            model_ref="scripted:m",
            permission_mode=PermissionMode.UNSAFE,
            console=Console(record=True, width=100, force_terminal=False),
        )
    )


def test_prompt_history_persists_and_browses(tmp_path: Path):
    path = tmp_path / "h.jsonl"
    h = PromptHistory(tmp_path, path=path)
    h.add("first task")
    h.add("second task")
    h.add("second task")
    h.add("/status")
    assert h.entries == ["first task", "second task"]
    assert h.previous("draft") == "second task" and h.browsing
    assert h.previous("") == "first task" and h.previous("") == "first task"
    assert h.next() == "second task" and h.next() == "draft" and not h.browsing
    again = PromptHistory(tmp_path, path=path)
    assert again.entries == ["first task", "second task"]
    assert again.next() is None


def test_command_catalog_from_help_and_custom(project: Path, _trendlab_home: Path):
    tl = _tl(project).tl
    names = dict(command_catalog(tl))
    assert names["/model"].startswith("Pick a model") and names["/commit"] and names["/stop"]
    assert "/branch" in names and "/mouse" in names and "/telegram" in names


async def test_tui_slash_menu_history_and_layout(project: Path, _trendlab_home: Path):
    tui = _tl(project)
    async with tui.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        inp = tui.query_one("#input", PromptInput)
        inp.focus()
        menu = tui.query_one("#cmdmenu", CommandPicker)
        await pilot.press(*"/mo")
        await pilot.pause()
        assert menu.has_class("visible") and menu.choice() in {
            "/model",
            "/models",
            "/mode",
            "/mouse",
        }
        await pilot.press("down")
        await pilot.pause()
        second = menu.choice()
        await pilot.press("tab")
        await pilot.pause()
        assert inp.text == second + " " and not menu.has_class("visible")
        inp.text = ""
        await pilot.pause()
        await pilot.press(*"/statu")
        await pilot.pause()
        assert menu.choice() == "/status"
        await pilot.press("enter")
        await pilot.pause()  # completes, does not run
        assert inp.text == "/status " and not menu.has_class("visible")
        await pilot.press("enter")
        await pilot.pause()  # now runs
        log_text = "\n".join(str(line) for line in tui.query_one("#transcript").lines)
        assert "Model" in log_text or "Mode" in log_text
        # History: two prompts, then Ctrl+↑ recalls them (mouse released mode).
        for text in ("explain this repo", "list the tests"):
            inp.text = text
            await pilot.press("enter")
            for _ in range(100):
                await pilot.pause(0.02)
                if tui._run_task and tui._run_task.done():
                    break
        assert tui.history.entries[-2:] == ["explain this repo", "list the tests"]
        await pilot.press("ctrl+up")
        await pilot.pause()
        assert inp.text == "list the tests"
        await pilot.press("ctrl+up")
        await pilot.pause()
        assert inp.text == "explain this repo"
        await pilot.press("ctrl+down", "ctrl+down")
        await pilot.pause()
        assert inp.text == ""
        # Mouse captured → plain ↑ is history on a single-line prompt.
        tui.set_mouse_capture(True)
        await pilot.press("up")
        await pilot.pause()
        assert inp.text == "list the tests"
        tui.set_mouse_capture(False)
        inp.text = ""
        # Plan panel hidden while there is no plan; header full-size at 40 rows.
        assert tui.query_one("#plan", Static).display is False
        assert not tui.query_one("#header", Static).has_class("compact")


async def test_tui_compact_layout_on_small_terminal(project: Path, _trendlab_home: Path):
    tui = _tl(project)
    async with tui.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        header = tui.query_one("#header", Static)
        assert header.has_class("compact") and header.size.height == 1
        assert "TRENDLAB" in str(header.content) and "scripted:m" in str(header.content)
        assert tui.query_one("#plan", Static).display is False
        status = str(tui.query_one("#status", Static).content)
        assert "drag selects" not in status  # trimmed for width
        assert tui.query_one("#transcript").size.height >= 14


def test_update_check_runs_off_the_startup_path():
    import inspect

    from trendlab import cli

    src = inspect.getsource(cli.main_callback)
    assert "_update_hint()" not in src
