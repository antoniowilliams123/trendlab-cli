"""Regenerate the UX screenshots in docs/screenshots/ with a scripted provider (no API calls).

    TRENDLAB_HOME=/tmp/tlh TRENDLAB_NO_UPDATE_CHECK=1 .venv/bin/python scripts/ux_screenshots.py

Renders the transcript after a mixed run (reads, a shell call, a denied sudo, an invalid edit),
the compact 80×24 layout, the slash-command menu and the model picker.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import cairosvg
from rich.console import Console

from trendlab.app import TrendLabApp
from trendlab.config.loader import load_config
from trendlab.config.schema import RemoteApprovalConfig
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.ui.tui import PromptInput, TrendLabTUI

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"


def make_tui() -> TrendLabTUI:
    cfg = load_config(ROOT)
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    cfg.telegram_bridge.enabled = False
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="1", name="shell", arguments={"command": "ls README.md pyproject.toml"}
                    ),
                    ToolCall(id="2", name="read_file", arguments={"path": "pyproject.toml"}),
                    ToolCall(id="3", name="shell", arguments={"command": "sudo whoami"}),
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="4", name="patch_file", arguments={"path": "BUILD_STATUS.md", "old": "x"}
                    )
                ]
            ),
            ModelResponse(
                text="The repo has a README and a pyproject.\n\n"
                '```toml\nname = "trendlab-cli"\n```\n\nNothing was changed.'
            ),
        ]
    )
    app = TrendLabApp(
        ROOT,
        cfg,
        provider=provider,
        model_ref="scripted:m",
        console=Console(record=True, width=100, force_terminal=False),
    )
    return TrendLabTUI(app)


def save(tui: TrendLabTUI, name: str, title: str) -> None:
    svg = tui.export_screenshot(title=title)
    svg = re.sub(r"font-family:[^;\"]+", "font-family: DejaVu Sans Mono, monospace", svg)
    cairosvg.svg2png(bytestring=svg.encode(), write_to=str(OUT / f"{name}.png"), output_width=1400)
    print("wrote", OUT / f"{name}.png")


async def transcript(size: tuple[int, int], name: str) -> None:
    tui = make_tui()
    async with tui.run_test(size=size) as pilot:
        await pilot.pause()
        box = tui.query_one("#input", PromptInput)
        box.focus()
        box.text = "what is in this repo?"
        await pilot.press("enter")
        for _ in range(300):
            await pilot.pause(0.05)
            if tui._run_task and tui._run_task.done():
                break
        await pilot.pause(0.3)
        save(tui, name, f"TrendLab {size[0]}x{size[1]}")


async def overlay(keys: str, name: str, title: str) -> None:
    tui = make_tui()
    async with tui.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        tui.query_one("#input", PromptInput).focus()
        await pilot.press(*keys)
        await pilot.pause()
        save(tui, name, title)


async def main() -> None:
    os.environ.setdefault("TRENDLAB_NO_UPDATE_CHECK", "1")
    await transcript((132, 40), "transcript_activity")
    await transcript((80, 24), "compact_80x24")
    await overlay("/mo", "slash_menu", "TrendLab — slash-command menu")
    await overlay("f5", "model_picker", "TrendLab — model picker")


if __name__ == "__main__":
    asyncio.run(main())
