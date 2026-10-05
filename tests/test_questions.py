import asyncio

import pytest

from trendlab.approvals.models import ApprovalError, ApprovalStatus
from trendlab.approvals.notifications.base import build_notification
from trendlab.config.schema import PermissionMode, RemoteApprovalConfig
from trendlab.permissions.engine import PermissionEngine
from trendlab.providers.base import ToolCall
from trendlab.telemetry.events import EventType
from trendlab.tools.ask_user import AskUserTool
from trendlab.tools.base import ToolContext
from trendlab.tools.registry import default_registry
from trendlab.tools.runtime import ToolRuntime

from .conftest import make_perm, run_request


async def _wait(mgr):
    for _ in range(200):
        await asyncio.sleep(0.01)
        if mgr.pending():
            return mgr.pending()[0]
    raise AssertionError("nothing pending")


async def test_ask_user_answered_from_phone(web, recorder):
    manager, channel, client = web
    engine = PermissionEngine(PermissionMode.ASK)
    ctx = ToolContext(project_root=manager.store.path.parent, session_id=manager.session_id)
    reg = default_registry()
    reg.register(AskUserTool(manager))
    rt = (
        ToolRuntime(reg, engine, manager, None or manager, manager.events, ctx)
        if False
        else ToolRuntime(reg, engine, manager, manager.events, ctx)
    )
    task = asyncio.create_task(
        rt.execute(
            ToolCall(
                id="1",
                name="ask_user",
                arguments={"question": "Which database?", "options": ["sqlite", "postgres"]},
            )
        )
    )
    req = await _wait(manager)
    assert req.kind == "question" and req.options == ["sqlite", "postgres"] and req.remote_allowed
    listing = client.get("/api/approvals").json()
    [item] = listing["approvals"]
    assert (
        item["kind"] == "question"
        and item["explanation"] == "Which database?"
        and item["options"] == ["sqlite", "postgres"]
    )
    # A question cannot be "approved": decide() refuses it.
    r = client.post(
        f"/api/approvals/{item['approval_id']}/decision",
        json={
            "decision": "approve",
            "scope": "once",
            "decision_token": item["decision_token"],
            "fingerprint": item["fingerprint"],
        },
    )
    assert r.status_code == 400 and r.json()["error"] == "not_an_approval"
    # Wrong token → 403; empty answer → 400; good answer → 200, single use.
    r = client.post(
        f"/api/approvals/{item['approval_id']}/answer",
        json={"answer": "postgres", "decision_token": "bad"},
    )
    assert r.status_code == 403
    r = client.post(
        f"/api/approvals/{item['approval_id']}/answer",
        json={"answer": "  ", "decision_token": item["decision_token"]},
    )
    assert r.status_code == 400
    r = client.post(
        f"/api/approvals/{item['approval_id']}/answer",
        json={"answer": "postgres", "decision_token": item["decision_token"]},
    )
    assert r.status_code == 200 and r.json()["status"] == "answered"
    res = await task
    assert res.ok and res.output == "USER ANSWER: postgres" and res.data["answer"] == "postgres"
    r = client.post(
        f"/api/approvals/{item['approval_id']}/answer",
        json={"answer": "again", "decision_token": item["decision_token"]},
    )
    assert r.status_code == 409
    asked = recorder.of_type(EventType.QUESTION_ASKED)[0].data
    answered = recorder.of_type(EventType.QUESTION_ANSWERED)[0].data
    assert (
        asked["question"] == "Which database?"
        and answered["answer"] == "postgres"
        and answered["channel"] == "web"
    )
    row = manager.store.get_approval(item["approval_id"])
    assert (
        row["status"] == "approved"
        and row["resolution_reason"] == "postgres"
        and row["decided_via"] == "web"
    )


async def test_ask_user_local_answer_and_expiry(manager_factory, clock):
    mgr = manager_factory(RemoteApprovalConfig(enabled=False))
    task = asyncio.create_task(mgr.ask("Proceed with migration?", ["yes", "no"]))
    req = await _wait(mgr)
    with pytest.raises(ApprovalError) as exc:
        mgr.decide(req.approval_id, "approve", via="local", trusted=True)
    assert exc.value.code == "not_an_approval"
    res = mgr.answer(req.approval_id, "no", via="local", by="terminal", trusted=True)
    assert res.status == ApprovalStatus.APPROVED and res.reason == "no"
    assert (await task).reason == "no"
    # Expiry via the clock.
    task = asyncio.create_task(mgr.ask("Still there?"))
    req = await _wait(mgr)
    clock.advance(minutes=31)
    with pytest.raises(ApprovalError) as exc:
        mgr.answer(req.approval_id, "yes", via="local", trusted=True)
    assert exc.value.code == "expired"
    mgr.sweep_expired()
    assert (await task).status == ApprovalStatus.EXPIRED


def test_question_notification_body():
    from datetime import timedelta

    from trendlab.approvals.models import ApprovalRequest

    req = ApprovalRequest.from_permission_request(
        make_perm(),
        session_id="s",
        machine="ThinkPad",
        timeout=timedelta(minutes=30),
        remote_allowed=True,
        session_scope_allowed=False,
    )
    req.kind, req.explanation, req.options = "question", "Which branch?", ["main", "dev"]
    n = build_notification(req, "http://x", "proj")
    assert "Question for you" in n.title and "Which branch?" in n.body and "main / dev" in n.body


async def test_reminders_sent_once_past_fraction(manager_factory, clock, recorder):
    from .test_approval_manager import CapturingNotifier

    notifier = CapturingNotifier()
    mgr = manager_factory(notifier=notifier)
    task = await run_request(mgr, make_perm())
    assert len(notifier.sent) == 1
    assert await mgr.send_reminders(0.75) == []
    clock.advance(minutes=23)
    [req] = mgr.pending()
    assert await mgr.send_reminders(0.75) == [req.approval_id]
    assert "Reminder" in notifier.sent[1].title and await mgr.send_reminders(0.75) == []
    mgr.cancel(req.approval_id)
    await task
