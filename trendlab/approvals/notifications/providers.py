"""Concrete notification providers. All use httpx so tests can inject a MockTransport."""

from __future__ import annotations

import httpx

from trendlab.approvals.notifications.base import (
    Notification,
    NotificationError,
    NotificationProvider,
)
from trendlab.config.schema import (
    NtfyNotificationConfig,
    TelegramNotificationConfig,
    WebhookNotificationConfig,
)
from trendlab.security.secrets import resolve_secret


def _env(name: str | None) -> str | None:
    return resolve_secret(name) if name else None


class _HttpProvider(NotificationProvider):
    def __init__(self, timeout: float, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def _post(self, url: str, **kwargs) -> httpx.Response:
        try:
            resp = await self._client.post(url, **kwargs)
        except httpx.HTTPError as exc:
            raise NotificationError(
                f"{self.name}: network error: {exc.__class__.__name__}"
            ) from exc
        if resp.status_code >= 400:
            raise NotificationError(f"{self.name}: HTTP {resp.status_code}")
        return resp

    async def close(self) -> None:
        await self._client.aclose()


class TelegramProvider(_HttpProvider):
    name = "telegram"

    def __init__(
        self,
        cfg: TelegramNotificationConfig,
        timeout: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(timeout, client)
        self.cfg = cfg

    async def send(self, notification: Notification) -> None:
        token = _env(self.cfg.bot_token_env)
        if not token:
            raise NotificationError(f"telegram: env var {self.cfg.bot_token_env} is not set")
        if not self.cfg.chat_id:
            raise NotificationError("telegram: chat_id is not configured")
        text = f"*{_md_escape(notification.title)}*\n{_md_escape(notification.body)}"
        payload = {"chat_id": self.cfg.chat_id, "text": text, "parse_mode": "Markdown"}
        if notification.url:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": "Open approvals", "url": notification.url}]]
            }
        await self._post(f"{self.cfg.api_base}/bot{token}/sendMessage", json=payload)


def _md_escape(text: str) -> str:
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


class NtfyProvider(_HttpProvider):
    name = "ntfy"

    def __init__(
        self, cfg: NtfyNotificationConfig, timeout: float, client: httpx.AsyncClient | None = None
    ) -> None:
        super().__init__(timeout, client)
        self.cfg = cfg

    async def send(self, notification: Notification) -> None:
        if not self.cfg.topic:
            raise NotificationError("ntfy: topic is not configured")
        headers = {
            "Title": notification.title.encode("ascii", "ignore").decode(),
            "Priority": "high" if notification.priority == "high" else "default",
            "Tags": "warning" if notification.risk == "high" else "bell",
        }
        if notification.url:
            headers["Click"] = notification.url
        token = _env(self.cfg.token_env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        await self._post(
            f"{self.cfg.server.rstrip('/')}/{self.cfg.topic}",
            content=notification.body.encode("utf-8"),
            headers=headers,
        )


class WebhookProvider(_HttpProvider):
    name = "webhook"

    def __init__(
        self,
        cfg: WebhookNotificationConfig,
        timeout: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(timeout, client)
        self.cfg = cfg

    async def send(self, notification: Notification) -> None:
        if not self.cfg.url:
            raise NotificationError("webhook: url is not configured")
        headers = {}
        auth = _env(self.cfg.auth_header_env)
        if auth:
            headers["Authorization"] = auth
        await self._post(self.cfg.url, json=notification.model_dump(), headers=headers)
