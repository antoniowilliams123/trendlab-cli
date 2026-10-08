"""Custom status line: user command, JSON on stdin, one line shown; project config ignored."""

import json
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from trendlab.config.loader import global_config_path, update_global_config
from trendlab.ui.statusline import SAMPLE_SCRIPT, StatusLine, configured_command, render


def _tl(root: Path):
    return SimpleNamespace(
        session_id="s1",
        model_ref="deepseek:deepseek-flash",
        project_root=root,
        context=SimpleNamespace(
            status=lambda: {"estimated_tokens": 5000, "context_window": 100000}
        ),
        agent=None,
        costs=SimpleNamespace(total_usd=0.01234),
        engine=None,
        approvals=SimpleNamespace(pending=lambda: [1]),
    )


async def test_command_gets_session_json_and_first_line_is_shown(tmp_path: Path):
    out = tmp_path / "seen.json"
    line = StatusLine(f"cat > {out}; printf 'hello\\nsecond'", 2)
    text = await line.refresh(_tl(tmp_path))
    assert text == "hello"
    seen = json.loads(out.read_text())
    assert seen["model"] == "deepseek:deepseek-flash" and seen["context_pct"] == 5
    assert seen["cost_usd"] == 0.0123 and seen["pending_approvals"] == 1
    assert not line.due()  # throttled until the interval passes


async def test_failures_show_a_short_hint(tmp_path: Path):
    assert (await render("echo boom >&2; exit 3", {}, tmp_path)).startswith(
        "statusline: exit 3: boom"
    )
    assert "timed out" in await render("sleep 5", {}, tmp_path)


async def test_sample_script_prints_one_line(tmp_path: Path):
    script = tmp_path / "s.sh"
    script.write_text(SAMPLE_SCRIPT)
    script.chmod(0o755)
    text = await StatusLine(str(script), 2).refresh(_tl(tmp_path))
    assert "flash" in text and "ctx 5%" in text and "$0.012" in text and "1 waiting" in text


def test_only_the_users_own_config_sets_the_command(_trendlab_home: Path, project: Path):
    (project / ".trendlab").mkdir()
    (project / ".trendlab" / "config.toml").write_text('[ui]\nstatusline = "touch pwned"\n')
    assert configured_command()[0] == ""  # a repository cannot choose a command to run
    update_global_config("ui", {"statusline": "echo mine", "statusline_interval_s": 5})
    assert configured_command() == ("echo mine", 5.0)


async def test_statusline_init_writes_a_script_and_turns_it_on(_trendlab_home: Path, project):
    from trendlab.ui.commands import CommandRouter

    console = Console(record=True, width=200)
    router = CommandRouter(SimpleNamespace(**vars(_tl(project))), console, quit_cb=lambda: None)
    await router._statusline(["init"])  # noqa: SLF001
    script = global_config_path().parent / "statusline.sh"
    assert script.is_file() and configured_command()[0] == str(script)
    out = console.export_text()
    assert "status line on" in out and "preview" in out and "ctx 5%" in out


async def test_tui_shows_the_custom_line_under_the_status_bar(_trendlab_home: Path, project):
    import asyncio

    from textual.widgets import Static

    from tests.test_phase2_ux import _tl as tui_for

    update_global_config("ui", {"statusline": "printf '\\033[35mmain\\033[0m · custom'"})
    tui = tui_for(project)
    async with tui.run_test(size=(120, 36)) as pilot:
        for _ in range(20):
            await pilot.pause(0.1)
            await asyncio.sleep(0)
            if tui.query_one("#statusline", Static).display:
                break
        line = tui.query_one("#statusline", Static)
        assert line.display is True and "main · custom" in str(line.content)


async def test_plain_repl_streams_new_lines_of_a_long_command(project: Path, _trendlab_home):
    from trendlab.app import TrendLabApp
    from trendlab.config.loader import load_config
    from trendlab.config.schema import PermissionMode, RemoteApprovalConfig
    from trendlab.providers.base import ModelResponse, ToolCall
    from trendlab.providers.scripted import ScriptedProvider
    from trendlab.ui.repl import Repl

    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    cmd = "for i in $(seq 1 30); do echo line$i; sleep 0.1; done"
    provider = ScriptedProvider(
        [
            ModelResponse(tool_calls=[ToolCall(id="s", name="shell", arguments={"command": cmd})]),
            ModelResponse(text="ran it"),
        ]
    )
    console = Console(record=True, width=120)
    tl = TrendLabApp(
        project,
        cfg,
        provider=provider,
        model_ref="scripted:m",
        permission_mode=PermissionMode.UNSAFE,
        console=console,
    )
    repl = Repl(tl, console)
    repl.LIVE_AFTER_S = 0.5
    await tl.start(interactive=False)
    tl.events.subscribe(repl._on_event)  # noqa: SLF001
    try:
        await tl.run_prompt("count")
    finally:
        await tl.stop()
    out = console.export_text()
    live = [ln for ln in out.splitlines() if ln.strip().startswith("│ line")]
    assert live, out[-800:]
    assert len(live) == len(set(live))  # each line printed once
    assert "│ line30" in out or "more lines" in out  # streamed through to the end
