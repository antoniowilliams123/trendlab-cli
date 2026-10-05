from pathlib import Path

from rich.console import Console
from typer.testing import CliRunner

from trendlab.app import TrendLabApp
from trendlab.cli import app
from trendlab.config.loader import load_config
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.ui.repl import Repl

runner = CliRunner()


def _app(project, cfg, provider, console=None, **kw):
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    return TrendLabApp(
        project,
        cfg,
        provider=provider,
        console=console or Console(record=True, width=120),
        model_ref="scripted:m",
        **kw,
    )


async def test_resume_in_place_and_export(project: Path, _trendlab_home: Path):
    cfg = load_config(project)
    console = Console(record=True, width=120)
    tl = _app(
        project,
        cfg,
        ScriptedProvider(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="1", name="task", arguments={"action": "plan", "titles": ["One"]}
                        )
                    ]
                ),
                ModelResponse(text="first session done; remaining task blocked"),
                ModelResponse(text="second session reply"),
            ]
        ),
        console,
    )
    repl = Repl(tl, console)
    await tl.start(interactive=False, command_handler=repl.handle_command)
    try:
        await tl.run_prompt("plan it")
        first = tl.session_id
        await repl.handle_command("/new")
        second = tl.session_id
        assert second != first and tl.plan.tasks == [] and tl.context.messages == []
        await tl.run_prompt("hello again")
        await repl.handle_command(f"/resume {first}")
        out = console.export_text(clear=True)
        assert "Resumed session" in out and tl.session_id == first and tl.resumed
        assert [t.title for t in tl.plan.tasks] == ["One"]
        assert tl.context.messages[0]["content"] == "plan it"
        assert tl.store.get_session(second)["status"] == "closed"
        await repl.handle_command("/resume nope")
        assert "no session" in console.export_text(clear=True)
        await repl.handle_command("/resume latest")
        assert tl.session_id == first  # already current → no-op
        path = tl.export_transcript()
        text = path.read_text()
        assert text.startswith(f"# TrendLab session {first}") and "**You:** plan it" in text
        assert "## Plan" in text and "tool `task`" in text and path.parent.name == "exports"
        await repl.handle_command("/export")
        assert "Exported" in console.export_text(clear=True)
    finally:
        await tl.stop()


def test_doctor_and_init(_trendlab_home: Path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    r = runner.invoke(
        app, ["init", "--provider", "deepseek"], input="sk-test-deepseek-key-123456\n"
    )
    assert r.exit_code == 0, r.output
    flat = " ".join(r.output.split())
    assert (
        "Stored DEEPSEEK_API_KEY" in flat
        and "deepseek:deepseek-flash" in flat
        and "sk-test" not in flat
    )
    cfg = load_config()
    assert (
        cfg.defaults.model == "deepseek:deepseek-flash"
        and cfg.providers["deepseek"].api_key_env == "DEEPSEEK_API_KEY"
    )
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 0
    flat = " ".join(r.output.split())
    assert "trendlab doctor" in flat and "DEEPSEEK_API_KEY: store" in flat and "UNSAFE" in flat
    r = runner.invoke(app, ["init", "--provider", "nope"])
    assert r.exit_code == 2
    r = runner.invoke(app, ["init"], input="2\nsk-ant-test-key-1234567890\n")
    assert r.exit_code == 0 and load_config().defaults.model == "anthropic:claude-opus-5"
