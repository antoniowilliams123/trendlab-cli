import asyncio

from typer.testing import CliRunner

from trendlab.cli import app, unsafe_banner
from trendlab.config.loader import load_config
from trendlab.config.schema import PermissionMode
from trendlab.permissions.engine import PermissionEngine
from trendlab.permissions.models import Decision, OperationCategory
from trendlab.providers.base import ToolCall
from trendlab.telemetry.events import EventType
from trendlab.tools.base import ToolContext
from trendlab.tools.registry import default_registry
from trendlab.tools.runtime import ToolRuntime

from .conftest import make_perm

C = OperationCategory


def test_unsafe_policy_table_and_hard_boundaries():
    eng = PermissionEngine(PermissionMode.UNSAFE)
    assert eng.unsafe
    for cat in (C.PROJECT_WRITE, C.FILE_DELETE, C.SHELL_WRITE, C.PACKAGE_INSTALL, C.NETWORK):
        v = eng.evaluate(make_perm(category=cat))
        assert v.decision == Decision.ALLOW and v.unsafe_auto and "AUTO" in v.reason, cat
    # Read-only things were already allowed in ask mode: not flagged as unsafe auto-approvals.
    assert not eng.evaluate(make_perm("ls", C.SHELL_READ)).unsafe_auto
    # Destructive still asks without the second flag.
    assert eng.evaluate(make_perm("rm -rf .", C.DESTRUCTIVE)).decision == Decision.ASK
    # Hard boundaries survive even here.
    assert eng.evaluate(make_perm("sudo x", C.PRIVILEGED)).decision == Decision.DENY
    assert eng.evaluate(make_perm("cat ../x", C.OUTSIDE_PROJECT)).decision == Decision.DENY
    eng2 = PermissionEngine(PermissionMode.UNSAFE, allow_destructive=True)
    v = eng2.evaluate(make_perm("rm -rf .", C.DESTRUCTIVE))
    assert v.decision == Decision.ALLOW and v.unsafe_auto
    assert (
        eng2.policy_table()[C.DESTRUCTIVE] == Decision.ALLOW
        and eng2.policy_table()[C.PRIVILEGED] == Decision.DENY
    )
    assert eng2.evaluate(make_perm("sudo rm -rf /", C.PRIVILEGED)).decision == Decision.DENY


def test_unsafe_is_the_default_and_configurable(_trendlab_home):
    assert load_config().defaults.permission_mode == PermissionMode.UNSAFE
    assert load_config().defaults.allow_destructive is False
    _trendlab_home.mkdir(parents=True, exist_ok=True)
    (_trendlab_home / "config.toml").write_text('[defaults]\npermission_mode = "ask"\n')
    assert load_config().defaults.permission_mode == PermissionMode.ASK
    (_trendlab_home / "config.toml").write_text("[defaults]\nallow_destructive = true\n")
    cfg = load_config()
    assert cfg.defaults.permission_mode == PermissionMode.UNSAFE and cfg.defaults.allow_destructive


async def test_unsafe_runs_without_prompts_and_audits(project, manager_factory, events, recorder):
    mgr = manager_factory()
    engine = PermissionEngine(PermissionMode.UNSAFE)
    ctx = ToolContext(project_root=project, session_id=mgr.session_id)
    rt = ToolRuntime(default_registry(), engine, mgr, events, ctx)
    res = await rt.execute(
        ToolCall(
            id="1",
            name="shell",
            arguments={"command": "pip install --help > /dev/null && touch installed.txt"},
        )
    )
    assert res.ok and (project / "installed.txt").exists() and mgr.pending() == []
    res = await rt.execute(
        ToolCall(id="2", name="write_file", arguments={"path": "n.py", "content": "x\n"})
    )
    assert res.ok and not recorder.of_type(EventType.APPROVAL_REQUESTED)
    audited = [
        e for e in recorder.of_type(EventType.PERMISSION_DECIDED) if e.data.get("unsafe_auto")
    ]
    assert len(audited) == 2 and audited[0].data["mode"] == "auto"
    assert audited[1].data["files"] == ["n.py"]
    # Destructive still asks (and denial keeps the file).
    task = asyncio.create_task(
        rt.execute(ToolCall(id="3", name="shell", arguments={"command": "rm -rf installed.txt"}))
    )
    for _ in range(100):
        await asyncio.sleep(0.01)
        if mgr.pending():
            break
    [req] = mgr.pending()
    assert not req.remote_allowed
    mgr.decide(req.approval_id, "deny", via="local", trusted=True)
    assert not (await task).ok and (project / "installed.txt").exists()
    # Boundaries.
    res = await rt.execute(
        ToolCall(id="4", name="shell", arguments={"command": "sudo touch /etc/x"})
    )
    assert res.output.startswith("DENIED")
    res = await rt.execute(
        ToolCall(id="5", name="read_file", arguments={"path": "../../etc/passwd"})
    )
    assert res.output.startswith("DENIED")


async def test_unsafe_requires_checkpoint_before_mutation(project, manager_factory, events):
    mgr = manager_factory()
    engine = PermissionEngine(PermissionMode.UNSAFE)
    ctx = ToolContext(project_root=project, session_id=mgr.session_id)
    rt = ToolRuntime(default_registry(), engine, mgr, events, ctx)

    async def broken_checkpoint(files):
        raise OSError("disk full")

    rt.on_before_mutation = broken_checkpoint
    res = await rt.execute(
        ToolCall(id="1", name="write_file", arguments={"path": "n.py", "content": "x"})
    )
    assert (
        not res.ok
        and "BLOCKED: checkpoint failed in AUTO mode" in res.output
        and not (project / "n.py").exists()
    )
    # In ordinary modes a checkpoint failure does not block the edit.
    engine.mode = PermissionMode.AUTO_EDIT
    res = await rt.execute(
        ToolCall(id="2", name="write_file", arguments={"path": "n.py", "content": "x"})
    )
    assert res.ok


def test_cli_flags_and_banner(project, _trendlab_home, monkeypatch):
    from trendlab import app as app_mod
    from trendlab.providers.base import ModelResponse
    from trendlab.providers.scripted import ScriptedProvider

    captured = {}
    orig = app_mod.TrendLabApp.__init__

    def patched(self, *a, **kw):
        kw["provider"] = ScriptedProvider([ModelResponse(text="ok")])
        kw["model_ref"] = "openai:m"
        captured.update(kw)
        orig(self, *a, **kw)

    monkeypatch.setattr(app_mod.TrendLabApp, "__init__", patched)

    def run(*args):
        r = CliRunner().invoke(app, ["-C", str(project), "-p", "hi", *args])
        return r.exit_code, " ".join(r.output.split())  # Rich wraps lines; compare flattened text

    # Default launch is unsafe: banner shown, mode left to config (unsafe).
    code, out = run()
    assert code == 0, out
    assert "AUTO MODE" in out and "still ask at the terminal" in out and "--safe" in out
    assert captured["permission_mode"] is None and captured["allow_destructive"] is False
    # --safe turns prompts on and silences the banner.
    code, out = run("--safe")
    assert (
        code == 0 and "AUTO MODE" not in out and captured["permission_mode"] == PermissionMode.ASK
    )
    code, out = run("--dangerously-skip-permissions", "--allow-destructive")
    assert code == 0 and "without asking" in out and captured["allow_destructive"] is True
    assert captured["permission_mode"] == PermissionMode.UNSAFE
    code, out = run("--safe", "--allow-destructive")
    assert code == 2 and "only applies in auto mode" in out
    assert "sudo" in unsafe_banner(False)


async def test_repl_switches_unsafe_off_and_on(project, _trendlab_home):
    from rich.console import Console

    from trendlab.app import TrendLabApp
    from trendlab.providers.scripted import ScriptedProvider
    from trendlab.ui.repl import Repl

    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    console = Console(record=True, width=120)
    tl = TrendLabApp(
        project,
        cfg,
        provider=ScriptedProvider([]),
        model_ref="scripted:m",
        console=console,
        permission_mode=PermissionMode.UNSAFE,
    )
    repl = Repl(tl, console)
    await tl.start(interactive=False, command_handler=repl.handle_command)
    try:
        await repl.handle_command("/permissions")
        assert "AUTO" in console.export_text(clear=True)
        await repl.handle_command("/mode ask")
        assert tl.engine.mode == PermissionMode.ASK
        await repl.handle_command("/mode unsafe")
        assert (
            "AUTO" in console.export_text(clear=True) and tl.engine.mode == PermissionMode.UNSAFE
        )
    finally:
        await tl.stop()
