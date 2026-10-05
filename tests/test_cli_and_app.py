from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from trendlab.app import TrendLabApp
from trendlab.approvals.tokens import load_or_create_token, rotate_token
from trendlab.cli import app
from trendlab.config.loader import load_config
from trendlab.config.schema import RemoteApprovalConfig
from trendlab.providers.base import ModelResponse
from trendlab.providers.scripted import ScriptedProvider
from trendlab.ui.repl import Repl

runner = CliRunner()


def test_version_and_help():
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0 and "TrendLab CLI 0.1.0" in r.output
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0 and "TrendLab CLI" in r.output and "Forge" not in r.output


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


def test_token_file_permissions_and_rotation(_trendlab_home: Path):
    t1 = load_or_create_token()
    assert len(t1) >= 32 and load_or_create_token() == t1
    path = _trendlab_home / "remote_token"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    t2 = rotate_token()
    assert t2 != t1 and load_or_create_token() == t2


async def test_app_wires_remote_and_restart_recovery(project: Path, _trendlab_home: Path):
    console = Console(record=True, width=120)
    cfg = load_config(project)
    cfg.remote_approval = RemoteApprovalConfig(enabled=True, host="127.0.0.1", port=0)
    provider = ScriptedProvider([ModelResponse(text="hi")])
    tl = TrendLabApp(project, cfg, provider=provider, console=console)
    await tl.start(interactive=False)
    try:
        status = tl.remote_status()
        assert status["enabled"] and status["url"].startswith("http://127.0.0.1:")
        assert tl.pairing_url().startswith(status["url"] + "/#t=")
        assert (await tl.agent.run("hello")) == "hi"
        # Leave a pending approval behind to simulate a crash.
        import asyncio

        from .conftest import make_perm, run_request

        task = await run_request(tl.approvals, make_perm())
        [req] = tl.approvals.pending()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        store_path = tl.store.path
    finally:
        tl.store.close()
        for ch in tl.approvals.channels:
            await tl.approvals.remove_channel(ch)
    # Second start: the stale approval is canceled, not re-armed.
    tl2 = TrendLabApp(project, cfg, provider=ScriptedProvider([]), console=console)
    await tl2.start(interactive=False)
    try:
        assert tl2.store.path == store_path
        assert tl2.approvals.pending() == []
        assert tl2.store.get_approval(req.approval_id)["status"] == "canceled"
        assert "Canceled 1 approval" in console.export_text()
        # Audit log was written and events persisted into SQLite as well.
        assert (_trendlab_home / "logs" / "events.jsonl").exists()
        assert tl2.store.events(tl2.session_id, "session.started")
    finally:
        await tl2.stop()


async def test_repl_commands(project: Path, _trendlab_home: Path):
    console = Console(record=True, width=120)
    cfg = load_config(project)
    cfg.remote_approval = RemoteApprovalConfig(enabled=False, host="127.0.0.1", port=0)
    tl = TrendLabApp(project, cfg, provider=ScriptedProvider([]), console=console)
    repl = Repl(tl, console)
    await tl.start(interactive=False, command_handler=repl.handle_command)
    try:
        await repl.handle_command("/remote status")
        assert "disabled" in console.export_text(clear=True)
        await repl.handle_command("/remote enable")
        out = console.export_text(clear=True)
        assert "Remote approval enabled" in out and "#t=" in out
        assert load_config().remote_approval.enabled is True  # persisted
        await repl.handle_command("/approvals")
        assert "no pending approvals" in console.export_text(clear=True)
        await repl.handle_command("/permissions")
        assert "package_install" in console.export_text(clear=True)
        await repl.handle_command("/mode trusted")
        assert tl.engine.mode.value == "trusted"
        await repl.handle_command("/remote disable")
        assert load_config().remote_approval.enabled is False
        await repl.handle_command("/bogus")
        assert "unknown command" in console.export_text(clear=True)
    finally:
        await tl.stop()
