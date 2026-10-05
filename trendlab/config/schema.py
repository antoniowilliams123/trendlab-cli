"""Configuration schema for TrendLab CLI.

Global config lives at ``~/.trendlab/config.toml``; a project may override
non-secret settings in ``.trendlab/config.toml``. Secrets are never stored in
these files — they are referenced by *environment variable name* only.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class PermissionMode(StrEnum):
    PLAN = "plan"
    ASK = "ask"
    AUTO_EDIT = "auto_edit"
    TRUSTED = "trusted"


class DefaultsConfig(BaseModel):
    model: str = "openai:gpt-4o-mini"
    permission_mode: PermissionMode = PermissionMode.ASK


class LimitsConfig(BaseModel):
    max_cost_usd: float | None = None
    max_iterations: int = 50


class ProviderConfig(BaseModel):
    type: str = "openai_compatible"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str | None = "OPENAI_API_KEY"
    default_model: str | None = None


class RemoteApprovalConfig(BaseModel):
    """Remote approval is disabled until explicitly configured."""

    enabled: bool = False
    channel: str = "web"
    host: str = "127.0.0.1"
    port: int = 8787
    # Public URL embedded in notifications (e.g. a Tailscale hostname). Defaults to host:port.
    public_url: str | None = None
    request_timeout_minutes: int = Field(default=30, ge=1, le=24 * 60)
    # High-risk / destructive operations stay local-only unless explicitly enabled.
    allow_high_risk: bool = False
    allow_session_scope: bool = True
    tls_cert: str | None = None
    tls_key: str | None = None
    # Binding a non-loopback address without TLS is refused unless this is set
    # (acceptable on an encrypted overlay such as Tailscale).
    allow_insecure_http: bool = False
    machine_name: str | None = None
    # Failed-auth lockout.
    max_auth_failures: int = 10

    @field_validator("channel")
    @classmethod
    def _channel(cls, v: str) -> str:
        if v not in {"web"}:
            raise ValueError(f"unknown remote approval channel {v!r} (supported: web)")
        return v


class TelegramNotificationConfig(BaseModel):
    bot_token_env: str = "TRENDLAB_TELEGRAM_BOT_TOKEN"
    chat_id: str | None = None
    api_base: str = "https://api.telegram.org"


class NtfyNotificationConfig(BaseModel):
    server: str = "https://ntfy.sh"
    topic: str | None = None
    token_env: str | None = None


class WebhookNotificationConfig(BaseModel):
    url: str | None = None
    auth_header_env: str | None = None


class NotificationsConfig(BaseModel):
    enabled: bool = False
    provider: str = "none"
    timeout_seconds: float = 10.0
    telegram: TelegramNotificationConfig = TelegramNotificationConfig()
    ntfy: NtfyNotificationConfig = NtfyNotificationConfig()
    webhook: WebhookNotificationConfig = WebhookNotificationConfig()

    @field_validator("provider")
    @classmethod
    def _provider(cls, v: str) -> str:
        if v not in {"none", "telegram", "ntfy", "webhook"}:
            raise ValueError(f"unknown notification provider {v!r}")
        return v


class AppConfig(BaseModel):
    defaults: DefaultsConfig = DefaultsConfig()
    limits: LimitsConfig = LimitsConfig()
    providers: dict[str, ProviderConfig] = Field(
        default_factory=lambda: {"openai": ProviderConfig()}
    )
    remote_approval: RemoteApprovalConfig = RemoteApprovalConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    # Project-level test/lint commands (optional).
    project: dict[str, str] = Field(default_factory=dict)
