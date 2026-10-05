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
    # No approval prompts (hard boundaries, destructive prompt, audit and checkpoint remain).
    UNSAFE = "unsafe"


class DefaultsConfig(BaseModel):
    model: str = "openai:gpt-4o-mini"
    # Tony's choice (2026-10-05): sessions start UNSAFE (no approval prompts) by default.
    # Switch off per session with `trendlab --safe` / `/mode ask`, or set "ask" here.
    permission_mode: PermissionMode = PermissionMode.UNSAFE
    # In UNSAFE mode, also run destructive commands (rm -rf, destructive git) without asking.
    allow_destructive: bool = False


class LimitsConfig(BaseModel):
    max_cost_usd: float | None = None
    max_iterations: int = 50
    # Read-only tool calls issued together by the model run concurrently, up to this many.
    parallel_tools: int = Field(default=6, ge=1, le=32)
    max_model_calls: int | None = None
    max_wall_clock_minutes: int | None = None
    # Warn when session cost reaches this fraction of max_cost_usd.
    warn_at_fraction: float = Field(default=0.8, ge=0.0, le=1.0)


class ContextConfig(BaseModel):
    max_file_bytes: int = 500_000
    auto_compact: bool = True
    # Compact when the estimated prompt exceeds this fraction of the model's context window.
    compact_threshold: float = Field(default=0.75, ge=0.1, le=0.95)
    default_context_window: int = 128_000
    repo_map_max_files: int = 400
    # Never compact a conversation smaller than this (tokens); prevents compaction churn.
    min_compaction_tokens: int = 1500
    repo_map_budget_tokens: int = 3_000
    recent_messages_budget_tokens: int = 60_000


class GitConfig(BaseModel):
    respect_gitignore: bool = True
    auto_commit: bool = False


class RetryConfig(BaseModel):
    max_attempts: int = Field(default=3, ge=1, le=10)
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 20.0


class ModelInfo(BaseModel):
    """Capability metadata for one provider:model reference (spec §58)."""

    context_window: int | None = None
    supports_tools: bool = True
    supports_streaming: bool = True
    local: bool | None = None  # inferred from provider when None


class ModelPricing(BaseModel):
    input_per_million: float = 0.0
    output_per_million: float = 0.0
    cached_input_per_million: float | None = None


class HookConfig(BaseModel):
    event: str
    command: str
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    # before_* hooks may block the operation by exiting non-zero when true.
    blocking: bool = False


class SandboxConfig(BaseModel):
    """OS sandbox for shell commands (bubblewrap). auto = use it when installed."""

    mode: str = "auto"  # auto | on | off
    allow_network: bool = False
    # Extra writable paths (e.g. a package cache). The project and /tmp are always writable.
    writable_paths: list[str] = Field(default_factory=lambda: ["~/.cache"])

    @field_validator("mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v not in {"auto", "on", "off"}:
            raise ValueError("sandbox mode must be auto, on or off")
        return v


class DiagnosticsConfig(BaseModel):
    """Run linters/type checkers on files the agent just edited and feed results back."""

    enabled: bool = True
    timeout_seconds: int = 60
    # Override per extension: {".py": ["ruff check {files}"]}
    commands: dict[str, list[str]] = Field(default_factory=dict)


class McpServerConfig(BaseModel):
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # Permission category applied to every tool from this server.
    category: str = "network"
    timeout_seconds: int = 30


class ProviderConfig(BaseModel):
    type: str = "openai_compatible"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str | None = "OPENAI_API_KEY"
    default_model: str | None = None
    # Force the structured-JSON tool-calling fallback for every model of this provider.
    tool_calling: str = "auto"  # auto | native | structured
    timeout_seconds: float = 120.0
    # Anthropic-only knobs (type = "anthropic").
    max_tokens: int = 16000
    thinking: str = "auto"  # auto | adaptive | off
    effort: str | None = None  # low | medium | high | xhigh | max
    refusal_fallbacks: str = "auto"  # auto | on | off (server-side fallbacks on Fable 5.x / Opus 5)
    prompt_caching: bool = True  # Anthropic: cache_control breakpoints on system + latest turn

    @field_validator("tool_calling")
    @classmethod
    def _tool_calling(cls, v: str) -> str:
        if v not in {"auto", "native", "structured"}:
            raise ValueError("tool_calling must be auto, native or structured")
        return v


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
    # Which runtime moments produce a phone notification.
    notify_on: list[str] = Field(
        default_factory=lambda: ["approval", "question", "completion", "failure"]
    )
    # Send a reminder when a pending approval reaches this fraction of its timeout (0 disables).
    reminder_at_fraction: float = Field(default=0.75, ge=0.0, le=1.0)
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
    context: ContextConfig = ContextConfig()
    git: GitConfig = GitConfig()
    retry: RetryConfig = RetryConfig()
    providers: dict[str, ProviderConfig] = Field(
        default_factory=lambda: {"openai": ProviderConfig()}
    )
    # Role → provider:model (spec §11). Roles: default, planning, explorer, debugger,
    # tester, reviewer, summarizer. Missing roles use the session model.
    routing: dict[str, str] = Field(default_factory=dict)
    # provider:model → ordered fallbacks used only for infrastructure failures (spec §69).
    fallback: dict[str, list[str]] = Field(default_factory=dict)
    models: dict[str, ModelInfo] = Field(default_factory=dict)
    pricing: dict[str, ModelPricing] = Field(default_factory=dict)
    hooks: list[HookConfig] = Field(default_factory=list)
    sandbox: SandboxConfig = SandboxConfig()
    diagnostics: DiagnosticsConfig = DiagnosticsConfig()
    mcp: dict[str, dict[str, McpServerConfig]] = Field(default_factory=dict)
    remote_approval: RemoteApprovalConfig = RemoteApprovalConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    # Project validation commands: test_command, lint_command, typecheck_command, build_command.
    project: dict[str, str] = Field(default_factory=dict)

    def mcp_servers(self) -> dict[str, McpServerConfig]:
        return self.mcp.get("servers", {})
