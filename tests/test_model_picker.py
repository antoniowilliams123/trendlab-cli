"""Model switching UX: catalog, fuzzy /model, the TUI picker (F5), the plain REPL list."""

from pathlib import Path

from rich.console import Console
from textual.widgets import Static

from trendlab.app import TrendLabApp
from trendlab.config.loader import load_config
from trendlab.config.schema import (
    ModelInfo,
    ModelPricing,
    PermissionMode,
    ProviderConfig,
    RemoteApprovalConfig,
)
from trendlab.providers.base import ModelResponse
from trendlab.providers.catalog import list_model_choices, resolve_model_query
from trendlab.providers.scripted import ScriptedProvider
from trendlab.ui.commands import CommandRouter
from trendlab.ui.tui import ModelPicker, PromptInput, TrendLabTUI


def _cfg(project: Path):
    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    cfg.providers["deepseek"] = ProviderConfig(
        type="openai_compatible",
        base_url="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
    )
    cfg.providers["ollama"] = ProviderConfig(type="ollama", base_url="http://localhost:11434/v1")
    cfg.providers["anthropic"] = ProviderConfig(type="anthropic", api_key_env="ANTHROPIC_API_KEY")
    cfg.models["deepseek:deepseek-flash"] = ModelInfo(context_window=1_000_000)
    cfg.pricing["deepseek:deepseek-flash"] = ModelPricing(
        input_per_million=0.30, output_per_million=1.20
    )
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    return cfg


def _tl(project: Path, provider):
    return TrendLabApp(
        project,
        _cfg(project),
        provider=provider,
        model_ref="scripted:m",
        permission_mode=PermissionMode.UNSAFE,
        console=Console(record=True, width=100, force_terminal=False),
    )


def test_catalog_merges_sources_and_marks_keys(project: Path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = _cfg(project)
    choices = list_model_choices(
        cfg, "deepseek:deepseek-flash", ollama_tags={"ollama": ["qwen2.5-coder:14b"]}
    )
    refs = [c.ref for c in choices]
    assert refs[0] == "deepseek:deepseek-flash" and choices[0].current  # current first
    assert "deepseek:deepseek-v4-pro" in refs  # curated
    assert "ollama:qwen2.5-coder:14b" in refs  # live-pulled
    assert "anthropic:claude-haiku-4-5" in refs and "openai:gpt-5-mini" in refs
    flash = choices[0]
    assert flash.ctx_label == "ctx 1M" and flash.price_label == "$0.30/$1.20 per M" and flash.key_ok
    qwen = next(c for c in choices if c.ref == "ollama:qwen2.5-coder:14b")
    assert (
        qwen.local
        and qwen.price_label == "free · local"
        and qwen.pulled
        and qwen.status_label == "LOCAL"
    )
    mini = next(c for c in choices if c.ref == "openai:gpt-5-mini")
    assert not mini.key_ok and mini.status_label == "key missing"
    assert resolve_model_query(choices, "deepseek:deepseek-flash").ref == "deepseek:deepseek-flash"
    assert resolve_model_query(choices, "haiku").ref == "anthropic:claude-haiku-4-5"
    mini = resolve_model_query(choices, "mini")  # openai and the scripted copy share a base_url
    assert isinstance(mini, list) and {c.ref for c in mini} == {
        "openai:gpt-5-mini",
        "scripted:gpt-5-mini",
    }
    assert resolve_model_query(choices, "v4-pro").ref == "deepseek:deepseek-v4-pro"
    many = resolve_model_query(choices, "deepseek")
    assert isinstance(many, list) and len(many) >= 2
    assert resolve_model_query(choices, "nope") == []


async def test_slash_model_fuzzy_switch_keeps_conversation(project: Path, _trendlab_home: Path):
    tl = _tl(project, ScriptedProvider([ModelResponse(text="one")]))
    await tl.start(interactive=False)
    try:
        await tl.run_prompt("hello")
        n = len(tl.context.messages)
        console = Console(record=True, width=120, force_terminal=False)
        router = CommandRouter(tl, console)
        await router.dispatch("/model flash")
        out = console.export_text(clear=True)
        assert (
            tl.model_ref == "deepseek:deepseek-flash"
            and "model →" in out
            and "conversation kept" in out
        )
        assert "ctx 1M" in out and "$0.30" in out
        assert len(tl.context.messages) == n  # history preserved
        await router.dispatch("/model deepseek")
        assert "ambiguous" in console.export_text(clear=True)
        await router.dispatch("/model zzz")
        assert "no model matches" in console.export_text(clear=True)
        await router.dispatch("/model scripted:m")
        assert tl.model_ref == "scripted:m"
    finally:
        await tl.stop()


async def test_tui_model_picker_filters_and_switches(project: Path, _trendlab_home: Path):
    tl = _tl(project, ScriptedProvider([ModelResponse(text="ok")]))
    tui = TrendLabTUI(tl)
    async with tui.run_test(size=(130, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f5")
        await pilot.pause()
        assert isinstance(tui.screen, ModelPicker)
        picker = tui.screen
        assert picker.shown[0].ref == "scripted:m"  # current first
        await pilot.press(*"flash")
        await pilot.pause()
        assert [c.ref for c in picker.shown] == ["deepseek:deepseek-flash"]
        detail = str(tui.screen.query_one("#model-detail", Static).content)
        assert "remote API" in detail and "ctx 1M" in detail
        await pilot.press("enter")
        await pilot.pause()
        assert not isinstance(tui.screen, ModelPicker)
        assert tl.model_ref == "deepseek:deepseek-flash"
        assert "deepseek-flash" in str(tui.query_one("#header", Static).content)
        # Esc keeps the current model; /model with no args opens the same picker.
        tui.query_one("#input", PromptInput).text = "/model"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(tui.screen, ModelPicker)
        await pilot.press("down", "escape")
        await pilot.pause()
        assert not isinstance(tui.screen, ModelPicker) and tl.model_ref == "deepseek:deepseek-flash"


async def test_plain_repl_numbered_pick(project: Path, _trendlab_home: Path):
    tl = _tl(project, ScriptedProvider([ModelResponse(text="ok")]))
    await tl.start(interactive=False)
    try:
        lines = iter(["2\n"])

        class FakeInput:
            async def readline(self):
                return next(lines, None)

        tl.console_input = FakeInput()
        console = Console(record=True, width=140, force_terminal=False)
        router = CommandRouter(tl, console)
        await router.dispatch("/model")
        out = console.export_text(clear=True)
        assert "type a number" in out and "scripted:m ◀" in out
        choices = list_model_choices(tl.config, "scripted:m")
        assert tl.model_ref == choices[1].ref
    finally:
        await tl.stop()
