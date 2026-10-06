"""Custom slash commands, @file references, background processes, scrollable approval diff."""

import asyncio
from pathlib import Path

from rich.console import Console
from textual.containers import VerticalScroll
from textual.widgets import RichLog

from trendlab.app import TrendLabApp
from trendlab.config.loader import load_config
from trendlab.config.schema import PermissionMode, RemoteApprovalConfig
from trendlab.extensions.commands import CustomCommandLibrary, parse_command_file
from trendlab.permissions.engine import PermissionEngine
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.tools.background import BackgroundProcessManager
from trendlab.tools.base import ToolContext
from trendlab.tools.registry import default_registry
from trendlab.tools.runtime import ToolRuntime
from trendlab.ui.commands import CommandRouter
from trendlab.ui.file_refs import (
    FileIndex,
    current_at_token,
    expand_file_refs,
    find_file_refs,
    fuzzy_score,
)
from trendlab.ui.tui import ApprovalModal, FilePicker, PromptInput, TrendLabTUI


def _tl(project: Path, provider, mode=PermissionMode.UNSAFE):
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


# -- custom commands -------------------------------------------------------------------------------
def test_custom_commands_parse_render_and_override(project: Path, _trendlab_home: Path):
    glob_dir = _trendlab_home / "commands"
    glob_dir.mkdir(parents=True)
    (glob_dir / "sec.md").write_text("Global security review of $ARGUMENTS\n")
    (glob_dir / "Bad Name.md").write_text("ignored\n")
    proj_dir = project / ".trendlab" / "commands"
    proj_dir.mkdir(parents=True)
    (proj_dir / "sec.md").write_text(
        "---\ndescription: Project security review\nmodel: anthropic:claude-sonnet-5\n---\n"
        "Review @src/app.py for $1 problems. Extra: $2\n"
    )
    (proj_dir / "fix.md").write_text("# Fix the failing tests\nRun the tests and fix them.\n")
    lib = CustomCommandLibrary(project, _trendlab_home)
    names = [c.name for c in lib.list()]
    assert names == ["fix", "sec"]
    sec = lib.get("/sec")
    assert sec.source == "project" and sec.description == "Project security review"
    assert sec.model == "anthropic:claude-sonnet-5"
    assert lib.render("sec", "injection 'two words'") == (
        "Review @src/app.py for injection problems. Extra: two words"
    )
    # No placeholders: arguments are appended.
    assert lib.render("fix", "only test_x") == (
        "# Fix the failing tests\nRun the tests and fix them.\n\nonly test_x"
    )
    assert lib.get("nope") is None and parse_command_file(glob_dir / "Bad Name.md", "g") is None
    path = lib.scaffold("deploy")
    assert path == proj_dir / "deploy.md" and lib.get("deploy") is not None
    assert "/deploy" in lib.help_lines()[0]


async def test_router_runs_custom_command_as_prompt(project: Path, _trendlab_home: Path):
    (project / ".trendlab" / "commands").mkdir(parents=True)
    (project / ".trendlab" / "commands" / "greet.md").write_text("Say hello to $ARGUMENTS\n")
    tl = _tl(project, ScriptedProvider([ModelResponse(text="hi")]))
    console = Console(record=True, width=100, force_terminal=False)
    ran: list[str] = []

    async def prompt_cb(text: str) -> None:
        ran.append(text)

    await tl.start(interactive=False)
    try:
        router = CommandRouter(tl, console, prompt_cb=prompt_cb)
        await router.dispatch("/greet Tony Williams")
        assert ran == ["Say hello to Tony Williams"]
        await router.dispatch("/commands")
        out = console.export_text(clear=True)
        assert "/greet" in out and "project" in out
        await router.dispatch("/nothing-here")
        assert "unknown command" in console.export_text(clear=True)
        # Built-ins always win over a same-named custom command.
        (project / ".trendlab" / "commands" / "status.md").write_text("never\n")
        tl.custom_commands.reload()
        await router.dispatch("/status")
        assert ran == ["Say hello to Tony Williams"]
    finally:
        await tl.stop()


# -- @file references ------------------------------------------------------------------------------
def test_file_refs_expand_inside_project_only(project: Path, tmp_path: Path):
    (project / "src" / "big.bin").write_bytes(b"\x00\x01" * 100)
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    text = f"Compare @src/app.py with @README.md, list @src/ and skip @{outside} @missing.py"
    refs = find_file_refs(text, project)
    assert [p.name for _, p in refs] == ["app.py", "README.md", "src"]
    prompt, attached = expand_file_refs(text + " and @shot.png", project)
    assert attached == ["src/app.py", "README.md", "src"]
    assert "--- @src/app.py ---\n```\nTIMEOUT = 30\n```" in prompt
    assert "(directory listing)" in prompt and "big.bin" in prompt and "nope" not in prompt
    assert prompt.startswith(text)  # the tokens stay in place
    prompt, attached = expand_file_refs("Look at @src/big.bin", project)
    assert "binary file" in prompt and attached == ["src/big.bin"]
    assert expand_file_refs("no refs here, email me@example.com", project) == (
        "no refs here, email me@example.com",
        [],
    )


def test_fuzzy_ranking_and_token_detection(project: Path):
    (project / "tests").mkdir()
    (project / "tests" / "test_app.py").write_text("")
    (project / "src" / "application_settings.py").write_text("")
    idx = FileIndex(project)
    assert idx.search("app")[0] == "src/app.py"
    assert idx.search("tapp")[0] == "tests/test_app.py"
    assert idx.search("") and idx.search("zzzz") == []
    assert fuzzy_score("app", "src/app.py") > fuzzy_score("app", "src/application_settings.py")
    assert fuzzy_score("xq", "src/app.py") == 0
    assert current_at_token("fix @src/ap now", 10) == (4, "@src/ap")
    assert current_at_token("fix @src/ap now", 1) is None
    assert current_at_token("see @shot.png", 9) is None  # images belong to the attachments path
    assert current_at_token("@", 1) == (0, "@")


async def test_tui_file_picker_completes_with_tab(project: Path, _trendlab_home: Path):
    tl = _tl(project, ScriptedProvider([ModelResponse(text="ok")]))
    tui = TrendLabTUI(tl)
    async with tui.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        inp = tui.query_one("#input", PromptInput)
        picker = tui.query_one("#picker", FilePicker)
        assert not picker.has_class("visible")
        inp.focus()
        await pilot.press(*"explain @app")
        await pilot.pause()
        assert picker.has_class("visible") and picker.choice() == "src/app.py"
        await pilot.press("tab")
        await pilot.pause()
        assert inp.text == "explain @src/app.py "
        assert not picker.has_class("visible")
        await pilot.press(*"and @readm")
        await pilot.pause()
        assert picker.choice() == "README.md"
        await pilot.press("enter")  # Enter picks when the picker is open, it does not send
        await pilot.pause()
        assert inp.text == "explain @src/app.py and @README.md " and tui._run_task is None
        await pilot.press("tab")  # no picker open: plain indent
        assert inp.text.endswith("    ")


async def test_tui_approval_diff_is_scrollable(project: Path, _trendlab_home: Path):
    body = "".join(f"line {i}\n" for i in range(120))
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="1", name="write_file", arguments={"path": "big.txt", "content": body}
                    )
                ]
            ),
            ModelResponse(text="done; validation not possible here"),
        ]
    )
    tl = _tl(project, provider, mode=PermissionMode.ASK)
    tui = TrendLabTUI(tl)
    async with tui.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        tui.query_one("#input", PromptInput).text = "write big.txt"
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.02)
            if isinstance(tui.screen, ApprovalModal):
                break
        assert isinstance(tui.screen, ApprovalModal)
        preview = tui.screen.query_one("#preview", VerticalScroll)
        assert preview.max_scroll_y > 0
        before = preview.scroll_y
        await pilot.press("j", "j", "pagedown")
        await pilot.pause()
        assert preview.scroll_y > before
        await pilot.press("k")
        await pilot.pause()
        await pilot.press("y")
        for _ in range(100):
            await pilot.pause(0.02)
            if tui._run_task and tui._run_task.done():
                break
        assert (project / "big.txt").read_text() == body
        log = tui.query_one("#transcript", RichLog)
        assert "✓ done" in "\n".join(str(line) for line in log.lines)


# -- background processes --------------------------------------------------------------------------
async def test_background_process_tool_lifecycle(project: Path, manager_factory, events):
    mgr = manager_factory()
    ctx = ToolContext(project_root=project, session_id=mgr.session_id)
    rt = ToolRuntime(default_registry(), PermissionEngine(PermissionMode.UNSAFE), mgr, events, ctx)

    async def call(**args):
        return await rt.execute(ToolCall(id="x", name="background_process", arguments=args))

    res = await call(
        action="start",
        name="ticker",
        command="i=0; while true; do echo tick $i; i=$((i+1)); sleep 0.05; done",
    )
    assert res.ok and res.output.startswith("started ") and res.data["running"]
    pid_id = res.data["id"]
    assert isinstance(ctx.background, BackgroundProcessManager)
    await asyncio.sleep(0.4)
    logs = await call(action="logs", id="ticker", lines=5)
    assert logs.ok and "tick" in logs.output and len(logs.output.splitlines()) <= 5
    status = await call(action="status", id=pid_id)
    assert status.ok and status.data["running"] is True
    listing = await call(action="list")
    assert pid_id in listing.output and "ticker" in listing.output
    stop = await call(action="stop", id="ticker")
    assert stop.ok and stop.data["running"] is False
    assert (await call(action="status", id=pid_id)).data["running"] is False
    bad = await call(action="start", command="definitely-not-a-command-xyz")
    assert not bad.ok and "exited immediately" in bad.output
    assert not (await call(action="logs", id="nope")).ok
    assert (project / ".trendlab" / "bg" / f"{pid_id}.log").exists()


async def test_background_processes_die_with_the_session(project: Path, _trendlab_home: Path):
    tl = _tl(project, ScriptedProvider([ModelResponse(text="ok")]))
    await tl.start(interactive=False)
    bp = await tl.background.start("sleep 30", cwd=project, name="sleeper")
    assert bp.running
    console = Console(record=True, width=120, force_terminal=False)
    router = CommandRouter(tl, console)
    await router.dispatch("/bg")
    assert "sleeper" in console.export_text(clear=True)
    await tl.stop()
    assert not bp.running


# -- mouse / copy-paste ------------------------------------------------------------------------------
async def test_tui_releases_mouse_by_default_and_keys_scroll_transcript(
    project: Path, _trendlab_home: Path
):
    tl = _tl(project, ScriptedProvider([ModelResponse(text="ok")]))
    tui = TrendLabTUI(tl)
    async with tui.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert tui.mouse_capture is False
        status = str(tui.query_one("#status").content)
        assert "drag selects" in status
        log = tui.query_one("#transcript", RichLog)
        for i in range(80):
            tui.log_line(f"line {i}")
        await pilot.pause()
        assert log.max_scroll_y > 0
        bottom = log.scroll_y
        inp = tui.query_one("#input", PromptInput)
        inp.focus()
        await pilot.press("up", "up", "up")  # wheel-up arrives as ↑ with the mouse released
        await pilot.pause()
        assert log.scroll_y < bottom
        after_up = log.scroll_y
        await pilot.press("pageup")
        await pilot.pause()
        assert log.scroll_y < after_up
        after_page = log.scroll_y
        await pilot.press("pagedown", "down", "down")
        await pilot.pause()
        assert log.scroll_y > after_page
        # Multi-line text: ↑ still moves the cursor when it can.
        inp.text = "a\nb"
        inp.move_cursor((1, 1))
        await pilot.press("up")
        assert inp.cursor_location[0] == 0
        await pilot.press("f4")
        await pilot.pause()
        assert tui.mouse_capture is True and "app" in str(tui.query_one("#status").content)
        inp.text = "/mouse off"
        await pilot.press("enter")
        await pilot.pause()
        assert tui.mouse_capture is False
        text = "\n".join(str(line) for line in log.lines)
        assert "mouse off" in text or "terminal" in text
