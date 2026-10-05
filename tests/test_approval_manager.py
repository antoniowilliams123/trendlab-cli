import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest

from trendlab.approvals.manager import ApprovalManager
from trendlab.approvals.models import ApprovalDecision, ApprovalError, ApprovalScope, ApprovalStatus
from trendlab.approvals.notifications.base import NotificationError, NotificationProvider
from trendlab.config.schema import RemoteApprovalConfig
from trendlab.permissions.models import OperationCategory, RiskLevel
from trendlab.telemetry.events import EventType, JsonlEventSink

from .conftest import make_perm, run_request


class FailingNotifier(NotificationProvider):
    name = "fake"

    async def send(self, notification):
        raise NotificationError("fake: provider down")


class CapturingNotifier(NotificationProvider):
    name = "capture"

    def __init__(self):
        self.sent = []

    async def send(self, notification):
        self.sent.append(notification)


async def test_request_creation_fields(manager_factory, recorder):
    mgr: ApprovalManager = manager_factory()
    task = await run_request(mgr, make_perm(explanation="need TA library", task_id="T-1"))
    [req] = mgr.pending()
    assert len(req.approval_id) >= 20 and req.session_id == mgr.session_id
    assert req.task_id == "T-1" and req.tool == "shell"
    assert req.category == OperationCategory.PACKAGE_INSTALL and req.risk == RiskLevel.MEDIUM
    assert req.command == "pip install pandas-ta" and req.cwd == "/work/trading-engine"
    assert req.machine == "ThinkPad" and req.status == ApprovalStatus.PENDING
    assert req.expires_at - req.created_at == timedelta(minutes=30)
    assert len(req.fingerprint) == 64 and req.explanation == "need TA library"
    assert req.remote_allowed and req.session_scope_allowed
    row = mgr.store.get_approval(req.approval_id)
    assert row["status"] == "pending" and row["fingerprint"] == req.fingerprint
    [ev] = recorder.of_type(EventType.APPROVAL_REQUESTED)
    assert ev.data["approval_id"] == req.approval_id and "decision_token" not in ev.data
    mgr.cancel(req.approval_id)
    result = await task
    assert result.status == ApprovalStatus.CANCELED


async def test_local_trusted_approval(manager_factory, recorder):
    mgr = manager_factory(RemoteApprovalConfig(enabled=False))
    task = await run_request(mgr, make_perm())
    [req] = mgr.pending()
    res = mgr.decide(
        req.approval_id, ApprovalDecision.APPROVE, via="local", by="terminal", trusted=True
    )
    assert res.approved and res.via == "local"
    assert (await task).approved
    assert recorder.of_type(EventType.APPROVAL_APPROVED)[0].data["channel"] == "local"
    assert mgr.store.get_approval(req.approval_id)["decided_via"] == "local"


async def test_denial(manager_factory, recorder):
    mgr = manager_factory()
    task = await run_request(mgr, make_perm())
    [req] = mgr.pending()
    mgr.decide(req.approval_id, "deny", via="local", trusted=True)
    result = await task
    assert result.status == ApprovalStatus.DENIED and not result.approved
    assert recorder.of_type(EventType.APPROVAL_DENIED)


async def test_expiration(manager_factory, recorder, clock):
    mgr = manager_factory()
    task = await run_request(mgr, make_perm(), timeout=timedelta(milliseconds=50))
    result = await task
    assert result.status == ApprovalStatus.EXPIRED
    assert recorder.of_type(EventType.APPROVAL_EXPIRED)
    assert mgr.pending() == []
    # A late decision is rejected as already decided.
    with pytest.raises(ApprovalError) as exc:
        mgr.decide(result.approval_id, "approve", via="local", trusted=True)
    assert exc.value.code == "already_decided"


async def test_clock_based_expiry_blocks_decision(manager_factory, clock):
    mgr = manager_factory()
    task = await run_request(mgr, make_perm())
    [req] = mgr.pending()
    clock.advance(minutes=31)
    with pytest.raises(ApprovalError) as exc:
        mgr.decide(req.approval_id, "approve", via="local", trusted=True)
    assert exc.value.code == "expired" and exc.value.http_status == 410
    assert (await task).status == ApprovalStatus.EXPIRED


async def test_replay_rejected(manager_factory, recorder):
    mgr = manager_factory()
    task = await run_request(mgr, make_perm())
    [req] = mgr.pending()
    mgr.decide(req.approval_id, "approve", via="local", trusted=True)
    with pytest.raises(ApprovalError) as exc:
        mgr.decide(req.approval_id, "approve", via="local", trusted=True)
    assert exc.value.code == "already_decided" and exc.value.http_status == 409
    assert (await task).approved
    assert recorder.of_type(EventType.APPROVAL_REJECTED)[0].data["code"] == "already_decided"


async def test_unknown_id(manager_factory):
    mgr = manager_factory()
    with pytest.raises(ApprovalError) as exc:
        mgr.decide("nope", "approve", via="web")
    assert exc.value.code == "unknown" and exc.value.http_status == 404


async def test_untrusted_requires_token_and_fingerprint(manager_factory):
    mgr = manager_factory()
    task = await run_request(mgr, make_perm())
    [req] = mgr.pending()
    with pytest.raises(ApprovalError) as exc:
        mgr.decide(
            req.approval_id,
            "approve",
            via="web",
            decision_token="wrong",
            fingerprint=req.fingerprint,
        )
    assert exc.value.code == "invalid_token"
    with pytest.raises(ApprovalError) as exc:
        mgr.decide(
            req.approval_id,
            "approve",
            via="web",
            decision_token=req.decision_token,
            fingerprint="0" * 64,
        )
    assert exc.value.code == "operation_mismatch"
    # Still pending after bad attempts; a correct decision then works.
    assert mgr.pending()
    res = mgr.decide(
        req.approval_id,
        "approve",
        via="web",
        decision_token=req.decision_token,
        fingerprint=req.fingerprint,
        by="10.0.0.2",
    )
    assert res.approved and res.by == "10.0.0.2"
    assert (await task).approved


async def test_simultaneous_requests_resolve_independently(manager_factory):
    mgr = manager_factory()
    t1 = await run_request(mgr, make_perm("pip install a"))
    t2 = await run_request(mgr, make_perm("pip install b"))
    t3 = await run_request(mgr, make_perm("pip install c"))
    pending = mgr.pending()
    assert [p.command for p in pending] == ["pip install a", "pip install b", "pip install c"]
    mgr.decide(pending[2].approval_id, "deny", via="local", trusted=True)
    mgr.decide(pending[0].approval_id, "approve", via="local", trusted=True)
    assert (await t3).status == ApprovalStatus.DENIED
    assert (await t1).approved
    assert len(mgr.pending()) == 1 and mgr.pending()[0].command == "pip install b"
    mgr.decide(pending[1].approval_id, "approve", "session", via="local", trusted=True)
    assert (await t2).scope == ApprovalScope.SESSION


async def test_same_operation_supersedes_older_pending(manager_factory, recorder):
    mgr = manager_factory()
    t1 = await run_request(mgr, make_perm())
    t2 = await run_request(mgr, make_perm())
    assert (await t1).status == ApprovalStatus.SUPERSEDED
    [req] = mgr.pending()
    mgr.decide(req.approval_id, "approve", via="local", trusted=True)
    assert (await t2).approved
    assert recorder.of_type(EventType.APPROVAL_SUPERSEDED)


async def test_high_risk_is_local_only_by_default(manager_factory, recorder):
    mgr = manager_factory()
    perm = make_perm(
        "rm -rf build", OperationCategory.DESTRUCTIVE, summary="Run DESTRUCTIVE command"
    )
    task = await run_request(mgr, perm, can_persist=False)
    [req] = mgr.pending()
    assert req.risk == RiskLevel.HIGH and not req.remote_allowed and not req.session_scope_allowed
    assert mgr.pending(remote_only=True) == []
    with pytest.raises(ApprovalError) as exc:
        mgr.decide(
            req.approval_id,
            "approve",
            via="web",
            decision_token=req.decision_token,
            fingerprint=req.fingerprint,
        )
    assert exc.value.code == "remote_not_allowed" and exc.value.http_status == 403
    # Session scope is refused even for the trusted local channel.
    with pytest.raises(ApprovalError) as exc:
        mgr.decide(req.approval_id, "approve", "session", via="local", trusted=True)
    assert exc.value.code == "scope_not_allowed"
    mgr.decide(req.approval_id, "approve", "once", via="local", trusted=True)
    assert (await task).approved


async def test_high_risk_remote_when_explicitly_allowed(manager_factory):
    mgr = manager_factory(RemoteApprovalConfig(enabled=True, allow_high_risk=True))
    perm = make_perm("rm -rf build", OperationCategory.DESTRUCTIVE)
    task = await run_request(mgr, perm, can_persist=False)
    [req] = mgr.pending(remote_only=True)
    assert req.remote_allowed and not req.session_scope_allowed
    mgr.decide(
        req.approval_id,
        "approve",
        via="web",
        decision_token=req.decision_token,
        fingerprint=req.fingerprint,
    )
    assert (await task).approved


async def test_notification_failure_does_not_block(manager_factory, recorder):
    mgr = manager_factory(notifier=FailingNotifier())
    task = await run_request(mgr, make_perm())
    [fail] = recorder.of_type(EventType.APPROVAL_DELIVERY_FAILED)
    assert fail.data["channel"] == "notify:fake" and "provider down" in fail.data["error"]
    [req] = mgr.pending()  # still pending, locally decidable
    mgr.decide(req.approval_id, "approve", via="local", trusted=True)
    assert (await task).approved


async def test_notification_sent_without_secrets(manager_factory, recorder, monkeypatch):
    monkeypatch.setenv("PYPI_TOKEN", "pypi-AgEIcHlwaS5vcmcSECRETSECRET")
    notifier = CapturingNotifier()
    mgr = manager_factory(notifier=notifier)
    perm = make_perm(
        "pip install x --index-url https://user:pypi-AgEIcHlwaS5vcmcSECRETSECRET@pypi.example/simple"
    )
    task = await run_request(mgr, perm)
    [n] = notifier.sent
    assert "SECRETSECRET" not in n.body and "[REDACTED]" in n.body
    assert "ThinkPad" in n.body and "trading-engine" in n.body and "Risk: Medium" in n.body
    assert n.url == "http://thinkpad:8787" and "decision_token" not in n.model_dump_json()
    assert recorder.of_type(EventType.APPROVAL_DISPATCHED)[-1].data["channel"] == "notify:capture"
    [req] = mgr.pending()
    assert "SECRETSECRET" not in (req.command or "")
    mgr.cancel(req.approval_id)
    await task


async def test_remote_disabled_means_no_notification_and_local_only(manager_factory, recorder):
    notifier = CapturingNotifier()
    mgr = manager_factory(RemoteApprovalConfig(enabled=False), notifier=notifier)
    assert mgr.remote_enabled is False
    task = await run_request(mgr, make_perm())
    assert notifier.sent == []
    assert recorder.of_type(EventType.APPROVAL_REQUESTED)[0].data["remote_enabled"] is False
    [req] = mgr.pending()
    mgr.decide(req.approval_id, "approve", via="local", trusted=True)
    assert (await task).approved


async def test_restart_cancels_stale_pending_without_executing(manager_factory, recorder, store):
    first = manager_factory(process_id="proc-1")
    task = await run_request(first, make_perm())
    [req] = first.pending()
    # Simulate a crash: the process dies with the approval pending.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get_approval(req.approval_id)["status"] == "pending"

    second = manager_factory(process_id="proc-2", session_id=first.session_id)
    stale = second.recover()
    assert [r["id"] for r in stale] == [req.approval_id]
    row = store.get_approval(req.approval_id)
    assert row["status"] == "canceled" and row["resolution_reason"] == "process_restart"
    assert recorder.of_type(EventType.APPROVAL_CANCELED)[-1].data["reason"] == "process_restart"
    # The old approval can never be "approved" into execution now.
    with pytest.raises(ApprovalError) as exc:
        second.decide(req.approval_id, "approve", via="local", trusted=True)
    assert exc.value.code == "already_decided"
    assert second.pending() == []


async def test_audit_trail_in_jsonl_and_sqlite(
    manager_factory, events, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("SOME_API_KEY", "zzz-top-secret-zzz")
    log = tmp_path / "events.jsonl"
    events.subscribe(JsonlEventSink(log))
    mgr = manager_factory()
    task = await run_request(mgr, make_perm("pip install x # SOME_API_KEY=zzz-top-secret-zzz"))
    [req] = mgr.pending()
    mgr.decide(
        req.approval_id,
        "approve",
        via="web",
        by="10.0.0.9 Safari",
        decision_token=req.decision_token,
        fingerprint=req.fingerprint,
    )
    await task
    records = [json.loads(line) for line in log.read_text().splitlines()]
    kinds = [r["event"] for r in records]
    assert kinds[:1] == ["approval.requested"]
    assert "approval.received" in kinds and "approval.approved" in kinds
    approved = next(r for r in records if r["event"] == "approval.approved")
    assert approved["channel"] == "web" and approved["client"] == "10.0.0.9 Safari"
    assert approved["approval_id"] == req.approval_id and approved["ts"]
    text = log.read_text()
    assert "zzz-top-secret-zzz" not in text and "decision_token" not in text
    row = mgr.store.get_approval(req.approval_id)
    assert (
        row["decided_via"] == "web" and row["decided_by"] == "10.0.0.9 Safari" and row["decided_at"]
    )
