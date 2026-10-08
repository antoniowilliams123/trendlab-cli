"""Readable output: newest streamed lines visible, messages land in the scrollable transcript
once, and reading above is never yanked to the bottom."""

from pathlib import Path

from textual.widgets import RichLog, Static

from tests.test_phase2_ux import _tl as tui_for
from trendlab.telemetry.events import Event, EventType


def _text(widget) -> str:
    return str(widget.content) if hasattr(widget, "content") else str(widget.renderable)


def _transcript_text(tui) -> str:
    return "\n".join(line.text for line in tui.query_one("#transcript", RichLog).lines)


async def test_live_pane_shows_the_newest_wrapped_lines(project: Path, _trendlab_home: Path):
    tui = tui_for(project)
    async with tui.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        for i in range(40):
            tui._on_token(f"line {i} " + "word " * 30 + "\n")  # noqa: SLF001
        await pilot.pause()
        shown = _text(tui.query_one("#stream", Static))
        assert "line 39" in shown and "line 0 " not in shown and shown.startswith("…")


async def test_messages_move_into_the_transcript_once(project: Path, _trendlab_home: Path):
    tui = tui_for(project)
    async with tui.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        tui._committed = ""  # noqa: SLF001
        tui._on_token("I'll look at your scripts first.")  # noqa: SLF001
        tui._handle_event(  # noqa: SLF001
            Event(type=EventType.MODEL_CALL_COMPLETED, data={"role": "main", "tool_calls": 2})
        )
        tui._on_token("**Done.** The script is ready.")  # noqa: SLF001
        tui._handle_event(  # noqa: SLF001
            Event(type=EventType.MODEL_CALL_COMPLETED, data={"role": "main", "tool_calls": 0})
        )
        await pilot.pause()
        text = _transcript_text(tui)
        assert "I'll look at your scripts first." in text
        assert "Done. The script is ready." in text and "**Done.**" not in text  # rendered
        assert tui._committed == "**Done.** The script is ready."  # noqa: SLF001
        assert tui.query_one("#stream", Static).has_class("visible") is False


async def test_reading_above_is_not_interrupted(project: Path, _trendlab_home: Path):
    tui = tui_for(project)
    async with tui.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for i in range(80):
            tui.log_line(f"row {i}")
        await pilot.pause()
        log = tui.query_one("#transcript", RichLog)
        assert log.scroll_y >= log.max_scroll_y - 1  # following the bottom
        tui.scroll_transcript(-20)
        await pilot.pause()
        before = log.scroll_y
        tui.log_line("new output while reading")
        await pilot.pause()
        assert log.scroll_y == before  # not yanked down
        tui.scroll_transcript(1000)
        tui.scroll_transcript(1000)
        await pilot.pause()
        tui.log_line("follows again at the bottom")
        await pilot.pause()
        assert log.scroll_y >= log.max_scroll_y - 1
