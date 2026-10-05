from __future__ import annotations

import httpx

from trendlab.approvals.notifications.base import NotificationProvider
from trendlab.approvals.notifications.providers import (
    NtfyProvider,
    TelegramProvider,
    WebhookProvider,
)
from trendlab.config.schema import NotificationsConfig


def create_notification_provider(
    cfg: NotificationsConfig, client: httpx.AsyncClient | None = None
) -> NotificationProvider | None:
    """Return the configured provider, or None when notifications are disabled."""
    if not cfg.enabled or cfg.provider == "none":
        return None
    if cfg.provider == "telegram":
        return TelegramProvider(cfg.telegram, cfg.timeout_seconds, client)
    if cfg.provider == "ntfy":
        return NtfyProvider(cfg.ntfy, cfg.timeout_seconds, client)
    if cfg.provider == "webhook":
        return WebhookProvider(cfg.webhook, cfg.timeout_seconds, client)
    raise ValueError(f"unknown notification provider {cfg.provider!r}")
