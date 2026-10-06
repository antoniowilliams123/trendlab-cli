"""Paste an image into the TUI like a hosted agent: Ctrl+V or a pasted image path attaches it."""

from pathlib import Path

from rich.console import Console
from textual.events import Paste
from textual.widgets import RichLog, Static

from trendlab.app import TrendLabApp
from trendlab.config.loader import load_config
from trendlab.config.schema import ModelInfo, PermissionMode, RemoteApprovalConfig
from trendlab.providers.base import ModelResponse
from trendlab.providers.catalog import supports_vision
from trendlab.providers.scripted import ScriptedProvider
from trendlab.ui.tui import PromptInput, TrendLabTUI

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da6364f8cfc000000301010018dd8db00000000049454e44ae426082"
)


def _tui(project: Path, provider):
    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    return TrendLabTUI(
        TrendLabApp(
            project,
            cfg,
            provider=provider,
            model_ref="scripted:m",
            permission_mode=PermissionMode.UNSAFE,
            console=Console(record=True, width=100, force_terminal=False),
        )
    )


def test_vision_guess_and_override(project: Path):
    cfg = load_config(project)
    assert supports_vision(cfg, "anthropic:claude-sonnet-5") and supports_vision(
        cfg, "openai:gpt-5-mini"
    )
    assert not supports_vision(cfg, "deepseek:deepseek-flash") and not supports_vision(
        cfg, "ollama:qwen2.5-coder:14b"
    )
    assert supports_vision(cfg, "ollama:llava:13b") and supports_vision(cfg, "ollama:gemma4:latest")
    cfg.models["deepseek:deepseek-flash"] = ModelInfo(supports_vision=True)
    assert supports_vision(cfg, "deepseek:deepseek-flash")


async def test_ctrl_v_and_pasted_path_attach_images(
    project: Path, _trendlab_home: Path, tmp_path: Path, monkeypatch
):
    shot = tmp_path / "clip.png"
    shot.write_bytes(PNG)
    import trendlab.ui.attachments as att

    monkeypatch.setattr(att, "grab_clipboard_image", lambda dest_dir=None: shot)
    provider = ScriptedProvider([ModelResponse(text="I see a 1x1 image")])
    tui = _tui(project, provider)
    async with tui.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        inp = tui.query_one("#input", PromptInput)
        inp.focus()
        await pilot.press("alt+v")  # the key Windows Terminal lets through
        await pilot.pause()
        assert tui.tl.pending_images == [shot]
        tui.tl.pending_images.clear()
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert tui.tl.pending_images == [shot]
        status = str(tui.query_one("#status", Static).content)
        assert "1 image" in status
        log = "\n".join(str(ln) for ln in tui.query_one("#transcript", RichLog).lines)
        assert "image attached" in log and "clip.png" in log
        assert "cannot see images" in log  # scripted:m is not a vision model → warned
        # A pasted path to an image file attaches instead of becoming text.
        shot2 = tmp_path / "second.jpg"
        shot2.write_bytes(PNG)
        tui.post_message(Paste(str(shot2)))
        await pilot.pause()
        await pilot.pause()
        assert tui.tl.pending_images == [shot, shot2] and inp.text == ""
        # Plain text paste still inserts text.
        tui.post_message(Paste("hello paste"))
        await pilot.pause()
        await pilot.pause()
        assert inp.text == "hello paste"
        inp.text = "what is in these?"
        await pilot.press("enter")
        for _ in range(200):
            await pilot.pause(0.02)
            if tui._run_task and tui._run_task.done():
                break
        assert tui.tl.pending_images == []  # consumed by the run
        sent = provider.calls[0][-1]["content"]
        assert isinstance(sent, list) and [
            p["path"] for p in sent if p["type"] == "image_path"
        ] == [str(shot), str(shot2)]
        assert "I see a 1x1 image" in "\n".join(
            str(ln) for ln in tui.query_one("#transcript", RichLog).lines
        )
