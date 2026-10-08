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
    # No approval prompts (hard boundaries, the irreversible-command prompt, audit and the
    # pre-edit checkpoint remain). Shown as AUTO; "unsafe" is accepted as the old spelling.
    AUTO = "auto"
    UNSAFE = "auto"  # alias kept for code and configs written before the rename

    @classmethod
    def _missing_(cls, value):  # type: ignore[override]
        if isinstance(value, str) and value.lower() == "unsafe":
            return cls.AUTO
        return None


class DefaultsConfig(BaseModel):
    model: str = "openai:gpt-4o-mini"
    # Owner's choice (2026-10-05): sessions start in AUTO mode (no approval prompts).
    # Switch off per session with `trendlab --safe` / `/mode ask`, or set "ask" here.
    permission_mode: PermissionMode = PermissionMode.AUTO
    # In AUTO mode, also run irreversible commands (rm -rf, history-rewriting git) without asking.
    allow_destructive: bool = False  # config key kept; the flag is --allow-irreversible


class LimitsConfig(BaseModel):
    max_cost_usd: float | None = None
    max_iterations: int = 50
    # Read-only tool calls issued together by the model run concurrently, up to this many.
    parallel_tools: int = Field(default=6, ge=1, le=32)
    max_model_calls: int | None = None
    max_wall_clock_minutes: int | None = None
    # Iterations one plan step may take before the attempt counts as failed (spec §4.1).
    step_iterations: int = Field(default=12, ge=3, le=100)
    # Warn when session cost reaches this fraction of max_cost_usd.
    warn_at_fraction: float = Field(default=0.8, ge=0.0, le=1.0)
    max_tokens_per_run: int | None = Field(default=None, ge=1000)  # input + output tokens
    # Latency budget per route, seconds (U14): past it the agent is told once to wrap up with
    # the smallest complete result. A soft budget; max_wall_clock_minutes is the hard stop.
    latency_budget_s: dict[str, float] = Field(
        default_factory=lambda: {"question": 90, "small_fix": 300, "feature": 900, "risky": 900}
    )


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
    # Tiered tool output (cheap-model spec §2): per-tool token budgets for what the lead model
    # sees. Output above the budget is parsed into facts (Tier 1) + targeted detail (Tier 2);
    # the full text goes to .trendlab/traces/<call_id>.log for ``inspect_output``.
    tool_budgets: dict[str, int] = Field(
        default_factory=lambda: {
            "shell": 1500,
            "run_tests": 1200,
            "search_text": 1000,
            "list_directory": 600,
            "read_file": 2500,
            "web_fetch": 1500,
            "git_diff": 1500,
            "git_log": 800,
            "git_status": 600,
            "glob": 600,
        }
    )
    # Unparsed output larger than this (tokens) is handed to the screener model for tiering.
    screener_threshold_tokens: int = 3000
    # Lexical retrieval: offer the top-k code chunks for the request as hints at run start.
    # Off until a live A/B shows it pays (offline: defect file in top 3 for 81% of suite tasks).
    retrieval: bool = False
    retrieval_k: int = Field(default=3, ge=1, le=10)
    retrieval_max_chars: int = Field(default=3000, ge=500)
    # U16: "bm25" (lexical), "vector" (embeddings) or "hybrid" (rank fusion of both)
    retrieval_mode: str = "bm25"
    embeddings: str = ""  # provider:model, e.g. "ollama:nomic-embed-text"


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
    supports_vision: bool | None = None  # None = guess from the model family (catalog.py)


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

    mode: str = "auto"  # auto | on | off | docker
    allow_network: bool = False
    # docker mode: shell commands run in this pinned image with the project mounted at /work.
    docker_image: str = "python:3.12-slim"
    # Extra writable paths (e.g. a package cache). The project and /tmp are always writable.
    writable_paths: list[str] = Field(default_factory=lambda: ["~/.cache"])

    @field_validator("mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v not in {"auto", "on", "off", "docker"}:
            raise ValueError("sandbox mode must be auto, on, off or docker")
        return v


class DiagnosticsConfig(BaseModel):
    """Run linters/type checkers on files the agent just edited and feed results back."""

    enabled: bool = True
    timeout_seconds: int = 60
    # Override per extension: {".py": ["ruff check {files}"]}
    commands: dict[str, list[str]] = Field(default_factory=dict)


class VerificationConfig(BaseModel):
    """Verify-then-surface (cheap-model spec §3.2, §5): an independent verifier reviews the
    diff + validation log before a run that changed files is reported as done; optionally the
    author works in a throwaway worktree and only a verified diff reaches the working tree."""

    # required = a 'fail' verdict stops the run; advisory = findings are reported only; off.
    verifier: str = "required"
    max_rounds: int = Field(default=1, ge=0, le=3)  # 'fix' verdicts handed back to the lead
    # inplace = edit the working tree (default until M1); worktree = edit .trendlab/worktrees/.
    workspace: str = "inplace"
    # Fix-type tasks that change source must also add/change a test (or state a waiver).
    regression_gate: bool = True
    apply_timeout_seconds: int = 60
    # Risk gating (M1 finding): a small change that validated green and has its regression
    # test does not need the stronger model's review. Either threshold met → review runs.
    min_diff_lines: int = Field(default=80, ge=0)
    min_files: int = Field(default=3, ge=1)
    # A 'pass' the verifier is unsure of (confidence below this) is treated as 'fix': the author
    # gets one more look. 0 disables.
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    # U2: a second verifier model on the same evidence; disagreements are recorded
    # (verify.second_opinion) and a second-opinion 'fail' downgrades a 'pass' to 'fix'.
    second_opinion: str | None = None

    @field_validator("verifier")
    @classmethod
    def _verifier(cls, v: str) -> str:
        if v not in {"required", "advisory", "off"}:
            raise ValueError("verification.verifier must be required, advisory or off")
        return v

    @field_validator("workspace")
    @classmethod
    def _workspace(cls, v: str) -> str:
        if v not in {"inplace", "worktree"}:
            raise ValueError("verification.workspace must be inplace or worktree")
        return v


class PromptsConfig(BaseModel):
    """Per-model prompt layer (cheap-model spec §6.3)."""

    drivers: bool = True  # merge packaged + owner + project model notes into the system prompt


class PlannerConfig(BaseModel):
    """One call to the planning role turns a non-trivial task into verifiable steps (§3.3)."""

    enabled: bool = True
    max_calls: int = Field(default=3, ge=1, le=6)  # initial plan + re-plans per run
    # M1 finding: planning every medium prompt cost more than it saved; plan long briefs only.
    min_prompt_chars: int = 400


class AttemptsConfig(BaseModel):
    """Best-of-N candidate patches when a step's validation fails (§4.2)."""

    # "auto" = 3 for models under $1 per million input tokens (or local), else 1.
    best_of: int | str = "auto"
    cheap_price_per_m: float = 1.0

    @field_validator("best_of")
    @classmethod
    def _best_of(cls, v: int | str) -> int | str:
        if isinstance(v, str):
            if v != "auto":
                raise ValueError("attempts.best_of must be an integer or 'auto'")
            return v
        if not 1 <= v <= 6:
            raise ValueError("attempts.best_of must be between 1 and 6")
        return v


class EngineConfig(BaseModel):
    """The local engine daemon (cheap-model spec §7)."""

    projects: list[str] = Field(default_factory=list)  # roots to watch / sleep over
    watch_minutes: int = Field(default=60, ge=5)
    sleep_at: str = "02:30"  # local time, once per day
    meta_days: int = Field(default=1, ge=1)
    digest_minutes: int = Field(default=180, ge=15)  # Telegram digest cadence
    file_failed_runs: bool = True  # failed interactive runs and verifier fails become cards
    canary: bool = False  # nightly 10-task canary against the default model (U1/U3)
    canary_at: str = "03:30"
    canary_drop_alert: int = Field(default=2, ge=1)  # tasks lost vs the last canary → inbox card
    review_commits: bool = False  # review new commits on watched projects (U13, async review)
    review_minutes: int = Field(default=120, ge=15)


class EconomicsConfig(BaseModel):
    """Spend governance (U10): monthly budgets checked by the engine, alerted once per
    threshold per month (Telegram when configured, and an inbox card)."""

    monthly_budget_usd: float = Field(default=0.0, ge=0)  # all projects; 0 = no budget
    project_budgets: dict[str, float] = Field(default_factory=dict)  # path -> USD per month
    alert_at: list[float] = Field(default_factory=lambda: [0.8, 1.0])


class SamplingParams(BaseModel):
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)


def _default_sampling() -> dict[str, SamplingParams]:
    # Judging roles answer the same question the same way: temperature 0 (U18). The main agent
    # keeps the provider default; best-of-N candidates vary temperature on purpose.
    zero = SamplingParams(temperature=0.0)
    return {r: zero for r in ("router", "verifier", "reviewer", "judge", "screener")}


class ToolsConfig(BaseModel):
    """Which tools this project or session may use (U26). Empty allow = every tool."""

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class SessionsConfig(BaseModel):
    """Retention (U6): how long stored sessions and events are kept."""

    retention_days: int = Field(default=90, ge=1)
    keep_latest: int = Field(default=50, ge=1)
    # record every model response so sessions can be replayed deterministically (U20)
    record_cassettes: bool = True
    # one live run per checkout; a second one is refused (use --worktree to run in parallel)
    run_lock: bool = True


class GovernanceConfig(BaseModel):
    """Scope, bloat and dependency control (U5); communication, change scope, turn commits
    (U9)."""

    scope_check: bool = True
    max_files_fix: int = Field(default=4, ge=1)
    max_files_change: int = Field(default=12, ge=1)
    max_new_definitions_fix: int = Field(default=3, ge=0)
    dependency_gate: bool = True  # a dependency the task did not ask for must be justified
    test_strength_check: bool = True  # a new test must fail without the fix
    # communication constraints (U9): measured on every answer, enforced when set
    max_answer_words: int = Field(default=0, ge=0)  # 0 = no limit
    plain_language: bool = False  # short sentences, acronyms spelled out, no walls of text
    tone: str = ""  # e.g. "neutral and direct"
    communication_nudge: bool = True  # one rewrite round when a set constraint is broken
    # change-scope constraint: edits outside these globs are refused ([] = anywhere)
    change_allow: list[str] = Field(default_factory=list)
    # prompt-level commits: each completed turn with changes becomes a commit on the side
    # branch trendlab/turns/<session>; the working branch, index and HEAD are never touched
    commit_per_turn: bool = False
    # architecture rules (U24): "pkg.low -> pkg.high" import edges a change may not add
    forbid_imports: list[str] = Field(default_factory=list)


class PlanGateConfig(BaseModel):
    """Ask for a human 'go' (terminal, phone page or Telegram buttons) before the first change
    of every run. Off by default; independent of the permission mode."""

    enabled: bool = False
    timeout_minutes: int = Field(default=30, ge=1, le=24 * 60)


class TelegramBridgeConfig(BaseModel):
    """Remote control from Telegram (spec §90.12): messages in the configured chat become
    prompts/steering/answers; replies, reports and questions come back. Uses
    notifications.telegram for the bot token env + chat_id."""

    enabled: bool = False
    allow_commands: bool = True  # slash commands from the chat (permission/approval ones never)
    announce: bool = True  # "online"/"session ended" messages on start and stop
    send_answers: bool = True  # include the model's answer in the completion message


class MemoryConfig(BaseModel):
    """Project memory across sessions (spec §92.1): .trendlab/memory.md."""

    enabled: bool = True
    learn: bool = True  # extract facts after noteworthy runs (uses the summarizer role)
    max_entries: int = Field(default=60, ge=5, le=500)


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
    # Also deliver approvals/questions as Telegram messages with inline buttons (uses
    # notifications.telegram for the bot token env + chat_id). Decisions arrive via getUpdates.
    telegram: bool = False

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
    memory: MemoryConfig = MemoryConfig()
    plan_gate: PlanGateConfig = PlanGateConfig()
    verification: VerificationConfig = VerificationConfig()
    planner: PlannerConfig = PlannerConfig()
    prompts: PromptsConfig = PromptsConfig()
    engine: EngineConfig = EngineConfig()
    governance: GovernanceConfig = GovernanceConfig()
    sessions: SessionsConfig = SessionsConfig()
    economics: EconomicsConfig = EconomicsConfig()
    sampling: dict[str, SamplingParams] = Field(default_factory=_default_sampling)
    tools: ToolsConfig = ToolsConfig()
    attempts: AttemptsConfig = AttemptsConfig()
    telegram_bridge: TelegramBridgeConfig = TelegramBridgeConfig()
    mcp: dict[str, dict[str, McpServerConfig]] = Field(default_factory=dict)
    remote_approval: RemoteApprovalConfig = RemoteApprovalConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    # Project validation commands: test_command, lint_command, typecheck_command, build_command.
    project: dict[str, str] = Field(default_factory=dict)

    def mcp_servers(self) -> dict[str, McpServerConfig]:
        return self.mcp.get("servers", {})
