import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from trendlab.approvals.models import ApprovalRequest
from trendlab.approvals.notifications.base import NotificationError, build_notification
from trendlab.approvals.notifications.providers import (
    NtfyProvider,
    TelegramProvider,
    WebhookProvider,
)
from trendlab.approvals.notifications.registry import create_notification_provider
from trendlab.config.schema import (
    NotificationsConfig,
    NtfyNotificationConfig,
    TelegramNotificationConfig,
    WebhookNotificationConfig,
)

from .conftest import make_perm


def _request() -> ApprovalRequest:
    return ApprovalRequest.from_permission_request(
        make_perm(),
        session_id="s1",
        machine="ThinkPad",
        timeout=timedelta(minutes=30),
        remote_allowed=True,
        session_scope_allowed=True,
    )


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_telegram_payload(monkeypatch):
    monkeypatch.setenv("TG_TOKEN", "123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKlll")
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})

    p = TelegramProvider(
        TelegramNotificationConfig(bot_token_env="TG_TOKEN", chat_id="-100"), 5, _client(handler)
    )
    n = build_notification(_request(), "http://thinkpad:8787", "trading-engine")
    await p.send(n)
    assert seen["url"].endswith("/sendMessage") and "123456789:" in seen["url"]
    assert seen["body"]["chat_id"] == "-100" and "pip install pandas\\-ta" in seen["body"][
        "text"
    ].replace("-", "\\-")
    assert seen["body"]["reply_markup"]["inline_keyboard"][0][0]["url"] == "http://thinkpad:8787"
    assert "decision_token" not in json.dumps(seen["body"])


async def test_telegram_missing_env_fails_cleanly(monkeypatch):
    monkeypatch.delenv("TG_TOKEN", raising=False)
    p = TelegramProvider(
        TelegramNotificationConfig(bot_token_env="TG_TOKEN", chat_id="1"),
        5,
        _client(lambda r: httpx.Response(200)),
    )
    with pytest.raises(NotificationError, match="TG_TOKEN"):
        await p.send(build_notification(_request(), None, "p"))


async def test_ntfy_headers_and_http_error():
    def ok(req: httpx.Request):
        assert req.headers["Title"].startswith("TrendLab CLI")
        assert (
            req.headers["Click"] == "http://thinkpad:8787" and req.headers["Priority"] == "default"
        )
        return httpx.Response(200)

    p = NtfyProvider(NtfyNotificationConfig(topic="tl-approvals"), 5, _client(ok))
    await p.send(build_notification(_request(), "http://thinkpad:8787", "p"))
    bad = NtfyProvider(NtfyNotificationConfig(topic="t"), 5, _client(lambda r: httpx.Response(500)))
    with pytest.raises(NotificationError, match="HTTP 500"):
        await bad.send(build_notification(_request(), None, "p"))


async def test_webhook_network_error():
    def boom(req):
        raise httpx.ConnectError("down")

    p = WebhookProvider(WebhookNotificationConfig(url="https://hook.example/x"), 5, _client(boom))
    with pytest.raises(NotificationError, match="network error"):
        await p.send(build_notification(_request(), None, "p"))


def test_registry_disabled_by_default():
    assert create_notification_provider(NotificationsConfig()) is None
    assert create_notification_provider(NotificationsConfig(enabled=True, provider="none")) is None
    assert (
        create_notification_provider(NotificationsConfig(enabled=True, provider="ntfy")).name
        == "ntfy"
    )


def test_notification_body_for_local_only_request():
    req = _request()
    req.remote_allowed = False
    n = build_notification(req, "http://x", "p")
    assert n.url is None and "approved at the terminal" in n.body


def test_seconds_remaining_uses_now():
    req = _request()
    assert 0 < req.seconds_remaining(datetime.now(UTC)) <= 1800
