"""Outbound-only Telegram messages for the engine (digests). Never polls, so it can coexist
with an interactive session's bridge on the same bot."""

from __future__ import annotations

import httpx

from trendlab.config.schema import AppConfig
from trendlab.security.secrets import resolve_secret


def telegram_target(config: AppConfig) -> tuple[str, str, str] | None:
    tg = config.notifications.telegram
    token = resolve_secret(tg.bot_token_env)
    if not token or not tg.chat_id:
        return None
    return tg.api_base, token, str(tg.chat_id)


async def send_telegram(config: AppConfig, text: str) -> bool:
    target = telegram_target(config)
    if target is None:
        return False
    api_base, token, chat_id = target
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{api_base}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text[:3900], "disable_web_page_preview": True},
            )
            return resp.status_code == 200
    except httpx.HTTPError:
        return False
