import asyncio
import json
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from trendlab.app import TrendLabApp
from trendlab.approvals.tokens import load_or_create_token, rotate_token
from trendlab.cli import app
from trendlab.config.loader import load_config
from trendlab.config.schema import RemoteApprovalConfig
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.ui.repl import Repl

runner = CliRunner()


def test_version_and_help():
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0 and "TrendLab CLI 0.3.0" in r.output
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0 and "TrendLab CLI" in r.output and "Forge" not in r.output
    assert "--resume" in r.output and "--output" in r.output


def test_remote_enable_disable_url(_trendlab_home: Path):
    r = runner.invoke(
        app, ["remote", "enable", "--host", "127.0.0.1", "--port", "8799", "--timeout", "15"]
    )
    assert r.exit_code == 0, r.output
    cfg = load_config().remote_approval
    assert cfg.enabled and cfg.port == 8799 and cfg.request_timeout_minutes == 15
    assert "#t=" in r.output and "127.0.0.1:8799" in r.output
    r = runner.invoke(app, ["remote", "status"])
    assert "enabled" in r.output
    r = runner.invoke(app, ["remote", "disable"])
    assert r.exit_code == 0 and load_config().remote_approval.enabled is False
    r = runner.invoke(app, ["approvals"])
    assert r.exit_code == 0 and "Recent approvals" in r.output
    r = runner.invoke(app, ["sessions", "--all"])
    assert r.exit_code == 0 and "Sessions" in r.output


def test_token_file_permissions_and_rotation(_trendlab_home: Path):
    t1 = load_or_create_token()
    assert len(t1) >= 32 and load_or_create_token() == t1
    path = _trendlab_home / "remote_token"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    t2 = rotate_token()
    assert t2 != t1 and load_or_create_token() == t2


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


async def test_app_wires_remote_and_restart_recovery(project: Path, _trendlab_home: Path):
    console = Console(record=True, width=120)
    cfg = load_config(project)
    cfg.remote_approval = RemoteApprovalConfig(enabled=True, host="127.0.0.1", port=0)
    tl = _app(project, cfg, ScriptedProvider([ModelResponse(text="hi")]), console)
    await tl.start(interactive=False)
    try:
        status = tl.remote_status()
        assert status["enabled"] and status["url"].startswith("http://127.0.0.1:")
        assert tl.pairing_url().startswith(status["url"] + "/#t=")
        assert (await tl.run_prompt("hello")).text == "hi"
        from .conftest import make_perm, run_request

        task = await run_request(tl.approvals, make_perm())
        [req] = tl.approvals.pending()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Cancellation inside request() records the row as canceled (not left pending)...
        assert tl.store.get_approval(req.approval_id)["status"] == "canceled"
        # ...so simulate a hard crash by forcing it back to pending.
        tl.store.update_approval(req.approval_id, status="pending")
        store_path = tl.store.path
    finally:
        tl.store.close()
        for ch in tl.approvals.channels:
            await tl.approvals.remove_channel(ch)
    tl2 = _app(project, cfg, ScriptedProvider([]), console)
    await tl2.start(interactive=False)
    try:
        assert tl2.store.path == store_path
        assert tl2.approvals.pending() == []
        assert tl2.store.get_approval(req.approval_id)["status"] == "canceled"
        assert "Canceled 1 approval" in console.export_text()
        assert (_trendlab_home / "logs" / "events.jsonl").exists()
        assert tl2.store.events(tl2.session_id, "session.started")
    finally:
        await tl2.stop()


async def test_session_resume_restores_messages_plan_and_model(project: Path, _trendlab_home: Path):
    cfg = load_config(project)
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="c1", name="task", arguments={"action": "plan", "titles": ["A", "B"]}
                    )
                ]
            ),
            ModelResponse(text="planned; remaining tasks pending"),
        ]
    )
    tl = _app(project, cfg, provider)
    await tl.start(interactive=False)
    res = await tl.run_prompt("make a plan")
    sid = tl.session_id
    assert res.status == "COMPLETED" and len(tl.plan.tasks) == 2
    await tl.stop()

    tl2 = _app(
        project,
        cfg,
        ScriptedProvider([ModelResponse(text="resumed reply; remaining tasks are blocked")]),
        resume="latest",
    )
    await tl2.start(interactive=False)
    try:
        assert tl2.resumed and tl2.session_id == sid
        assert [t.title for t in tl2.plan.tasks] == ["A", "B"]
        roles = [m["role"] for m in tl2.context.messages]
        assert roles[:3] == ["user", "assistant", "tool"] and roles[-1] == "assistant"
        res = await tl2.run_prompt("continue")
        assert res.text.startswith("resumed reply")
        assert len(tl2.store.messages(sid)) == len(tl2.context.messages)
        assert tl2.store.usage(sid)["calls"] == 3
    finally:
        await tl2.stop()
    tl3 = _app(project, cfg, ScriptedProvider([]), resume="does-not-exist")
    await tl3.start(interactive=False)
    try:
        assert not tl3.resumed and tl3.session_id != sid
    finally:
        await tl3.stop()


async def test_auto_checkpoint_and_undo_via_app(project: Path, _trendlab_home: Path):
    cfg = load_config(project)
    from trendlab.config.schema import PermissionMode

    cfg.defaults.permission_mode = PermissionMode.AUTO_EDIT
    cfg.planner.design_checkpoint = False  # this test is about checkpoints, not design
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="patch_file",
                        arguments={"path": "src/app.py", "old_text": "30", "new_text": "60"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="c2", name="write_file", arguments={"path": "extra.py", "content": "e\n"}
                    )
                ]
            ),
            ModelResponse(text="edited (validation not possible here)"),
        ]
    )
    tl = _app(project, cfg, provider)
    await tl.start(interactive=False)
    try:
        res = await tl.run_prompt("edit")
        assert (
            res.status == "COMPLETED"
            and (project / "src" / "app.py").read_text() == "TIMEOUT = 60\n"
        )
        [cp] = tl.checkpoints.list()
        assert {e["path"] for e in cp["files"]} == {"src/app.py", "extra.py"} and cp["files"][0][
            "post_sha256"
        ]
        undo = tl.checkpoints.undo()
        assert undo["ok"] and (project / "src" / "app.py").read_text() == "TIMEOUT = 30\n"
        assert not (project / "extra.py").exists()
    finally:
        await tl.stop()


async def test_repl_commands(project: Path, _trendlab_home: Path):
    console = Console(record=True, width=120)
    cfg = load_config(project)
    cfg.remote_approval = RemoteApprovalConfig(enabled=False, host="127.0.0.1", port=0)
    tl = _app(project, cfg, ScriptedProvider([]), console)
    repl = Repl(tl, console)
    await tl.start(interactive=False, command_handler=repl.handle_command)
    try:
        await repl.handle_command("/remote status")
        assert "disabled" in console.export_text(clear=True)
        await repl.handle_command("/remote enable")
        out = console.export_text(clear=True)
        assert "Remote approval enabled" in out and "#t=" in out
        assert load_config().remote_approval.enabled is True
        await repl.handle_command("/approvals")
        assert "no pending approvals" in console.export_text(clear=True)
        await repl.handle_command("/permissions")
        assert "package_install" in console.export_text(clear=True)
        await repl.handle_command("/mode trusted")
        assert tl.engine.mode.value == "trusted"
        for cmd, expect in [
            ("/plan", "no plan yet"),
            ("/context", "context window"),
            ("/cost", "Session cost"),
            ("/cost-limit 2.5", "2.50"),
            ("/diff", "no files changed"),
            ("/checkpoint", "no checkpoints"),
            ("/sessions", "Sessions"),
            ("/models", "Providers"),
            ("/hooks", "no hooks"),
            ("/mcp", "no MCP servers"),
            ("/git status", ""),
            ("/status", "Context"),
            ("/undo", "no checkpoints"),
            ("/compact", "nothing to compact"),
            ("/help", "/review"),
        ]:
            await repl.handle_command(cmd)
            assert expect in console.export_text(clear=True), cmd
        await repl.handle_command("/new")
        assert "New session" in console.export_text(clear=True)
        await repl.handle_command("/remote disable")
        assert load_config().remote_approval.enabled is False
        await repl.handle_command("/bogus")
        assert "unknown command" in console.export_text(clear=True)
    finally:
        await tl.stop()


def test_headless_json_output(project: Path, _trendlab_home: Path, monkeypatch):
    """`trendlab -p ... --output json` prints a machine-readable report and exit code reflects status."""
    import trendlab.cli as cli_mod

    scripted = ScriptedProvider([ModelResponse(text="headless ok")])

    class FakeApp(cli_mod.__dict__["TrendLabApp"] if "TrendLabApp" in cli_mod.__dict__ else object):
        pass

    from trendlab import app as app_mod

    orig = app_mod.TrendLabApp.__init__

    def patched(self, *a, **kw):
        kw["provider"] = scripted
        kw["model_ref"] = "openai:m"
        orig(self, *a, **kw)

    monkeypatch.setattr(app_mod.TrendLabApp, "__init__", patched)
    r = runner.invoke(app, ["-C", str(project), "-p", "say hi", "--output", "json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output[r.output.index("{") :])
    assert data["status"] == "COMPLETED" and data["text"] == "headless ok" and "cost_usd" in data
