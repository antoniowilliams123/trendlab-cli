import asyncio
from pathlib import Path

from trendlab.approvals.manager import ApprovalManager
from trendlab.config.schema import PermissionMode, RemoteApprovalConfig
from trendlab.permissions.engine import PermissionEngine
from trendlab.permissions.models import Decision
from trendlab.providers.base import ToolCall
from trendlab.telemetry.events import EventType
from trendlab.tools.base import ToolContext
from trendlab.tools.registry import default_registry
from trendlab.tools.runtime import ToolRuntime


def _runtime(project: Path, mgr: ApprovalManager, events, mode=PermissionMode.ASK, states=None):
    engine = PermissionEngine(mode)
    ctx = ToolContext(project_root=project, session_id=mgr.session_id)
    rt = ToolRuntime(
        default_registry(),
        engine,
        mgr,
        events,
        ctx,
        state_hook=(states.append if states is not None else None),
    )
    return rt, engine


async def _approve_first_pending(mgr: ApprovalManager, decision="approve", scope="once", via="web"):
    for _ in range(50):
        await asyncio.sleep(0.01)
        if mgr.pending():
            break
    [req] = mgr.pending()
    if via == "web":
        return mgr.decide(
            req.approval_id,
            decision,
            scope,
            via="web",
            by="phone",
            decision_token=req.decision_token,
            fingerprint=req.fingerprint,
        )
    return mgr.decide(req.approval_id, decision, scope, via="local", trusted=True)


async def test_read_only_tool_runs_without_approval(project, manager_factory, events, recorder):
    rt, _ = _runtime(project, manager_factory(), events)
    res = await rt.execute(ToolCall(id="1", name="read_file", arguments={"path": "src/app.py"}))
    assert res.ok and "TIMEOUT = 30" in res.output
    assert not recorder.of_type(EventType.APPROVAL_REQUESTED)


async def test_outside_project_denied_by_engine(project, manager_factory, events):
    rt, _ = _runtime(project, manager_factory(), events)
    res = await rt.execute(
        ToolCall(id="1", name="read_file", arguments={"path": "../../.ssh/id_rsa"})
    )
    assert (
        not res.ok and res.output.startswith("DENIED") and res.data["category"] == "outside_project"
    )


async def test_sensitive_file_treated_as_outside(project, manager_factory, events):
    (project / ".env").write_text("SECRET=1")
    rt, _ = _runtime(project, manager_factory(), events)
    res = await rt.execute(ToolCall(id="1", name="read_file", arguments={"path": ".env"}))
    assert not res.ok and "DENIED" in res.output


async def test_privileged_command_denied_outright(project, manager_factory, events):
    rt, _ = _runtime(project, manager_factory(), events)
    res = await rt.execute(ToolCall(id="1", name="shell", arguments={"command": "sudo rm -rf /"}))
    assert not res.ok and "DENIED" in res.output


async def test_remote_approval_then_execution(project, manager_factory, events, recorder):
    states = []
    rt, _ = _runtime(project, manager_factory(), events, states=states)
    call = ToolCall(
        id="1", name="write_file", arguments={"path": "src/new.py", "content": "x = 1\n"}
    )
    task = asyncio.create_task(rt.execute(call))
    await _approve_first_pending(manager_factory and rt.approvals, via="web")
    res = await task
    assert res.ok and (project / "src" / "new.py").read_text() == "x = 1\n"
    assert states[:2] == ["WAITING_PERMISSION", "RUNNING_TOOL"]
    assert recorder.of_type(EventType.FILE_CHANGED)[0].data["files"] == ["src/new.py"]
    assert recorder.of_type(EventType.APPROVAL_APPROVED)[0].data["channel"] == "web"


async def test_denied_approval_does_not_execute(project, manager_factory, events):
    rt, _ = _runtime(project, manager_factory(), events)
    call = ToolCall(id="1", name="write_file", arguments={"path": "src/new.py", "content": "x"})
    task = asyncio.create_task(rt.execute(call))
    await _approve_first_pending(rt.approvals, decision="deny")
    res = await task
    assert not res.ok and "NOT EXECUTED: approval denied" in res.output
    assert not (project / "src" / "new.py").exists()


async def test_expired_approval_does_not_execute(project, manager_factory, events):
    mgr = manager_factory(RemoteApprovalConfig(enabled=True, request_timeout_minutes=1))
    rt, _ = _runtime(project, mgr, events)
    # Shrink the timeout through the manager's config for the test.
    from datetime import timedelta

    orig = mgr.request

    async def fast(perm, **kw):
        kw["timeout"] = timedelta(milliseconds=30)
        return await orig(perm, **kw)

    mgr.request = fast  # type: ignore[method-assign]
    res = await rt.execute(ToolCall(id="1", name="shell", arguments={"command": "touch made.txt"}))
    assert not res.ok and "approval expired" in res.output
    assert not (project / "made.txt").exists()


async def test_session_scope_adds_rule_and_skips_next_prompt(
    project, manager_factory, events, recorder
):
    rt, engine = _runtime(project, manager_factory(), events)
    call = ToolCall(id="1", name="shell", arguments={"command": "touch a.txt"})
    task = asyncio.create_task(rt.execute(call))
    await _approve_first_pending(rt.approvals, scope="session")
    assert (await task).ok
    assert engine.session_rules() == {"shell:shell_write:touch a.txt": Decision.ALLOW}
    res = await rt.execute(
        ToolCall(id="2", name="shell", arguments={"command": "touch a.txt b.txt"})
    )
    assert res.ok and (project / "b.txt").exists()
    assert len(recorder.of_type(EventType.APPROVAL_REQUESTED)) == 1


async def test_destructive_requires_once_only_local_approval(project, manager_factory, events):
    rt, engine = _runtime(project, manager_factory(), events)
    (project / "junk").mkdir()
    call = ToolCall(id="1", name="shell", arguments={"command": "rm -rf junk"})
    task = asyncio.create_task(rt.execute(call))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if rt.approvals.pending():
            break
    [req] = rt.approvals.pending()
    assert req.risk.value == "high" and not req.remote_allowed and not req.session_scope_allowed
    rt.approvals.decide(req.approval_id, "approve", via="local", trusted=True)
    assert (await task).ok and not (project / "junk").exists()
    assert engine.session_rules() == {}


async def test_binding_mismatch_at_execution_blocks(project, manager_factory, events, recorder):
    """If the stored approval does not match the operation about to run, nothing runs."""
    mgr = manager_factory()
    rt, _ = _runtime(project, mgr, events)
    call = ToolCall(id="1", name="shell", arguments={"command": "touch z.txt"})
    task = asyncio.create_task(rt.execute(call))
    await asyncio.sleep(0.02)
    [req] = mgr.pending()
    # Corrupt the persisted binding (simulates tampering / a bug between approval and execution).
    mgr.store.update_approval(req.approval_id, fingerprint="0" * 64)
    mgr.decide(req.approval_id, "approve", via="local", trusted=True)
    res = await task
    assert not res.ok and "does not match" in res.output
    assert not (project / "z.txt").exists()
    assert (
        recorder.of_type(EventType.APPROVAL_REJECTED)[-1].data["code"]
        == "binding_mismatch_at_execution"
    )


async def test_shell_output_is_redacted(project, manager_factory, events, monkeypatch):
    monkeypatch.setenv("DEMO_API_KEY", "verysecretapikey42")
    rt, _ = _runtime(project, manager_factory(), events, mode=PermissionMode.TRUSTED)
    res = await rt.execute(
        ToolCall(id="1", name="shell", arguments={"command": "echo $DEMO_API_KEY"})
    )
    assert res.ok and "verysecretapikey42" not in res.output and "[REDACTED]" in res.output


async def test_write_file_conflict_protection(project, manager_factory, events):
    rt, _ = _runtime(project, manager_factory(), events, mode=PermissionMode.AUTO_EDIT)
    res = await rt.execute(
        ToolCall(
            id="1",
            name="write_file",
            arguments={
                "path": "src/app.py",
                "content": "TIMEOUT = 60\n",
                "expected_sha256": "deadbeef",
            },
        )
    )
    assert not res.ok and "conflict" in res.output
    assert (project / "src" / "app.py").read_text() == "TIMEOUT = 30\n"
