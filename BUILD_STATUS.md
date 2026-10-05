# TrendLab Build Status

## Current Milestone
Spec V1 implemented end to end (2026-10-04). Remaining work is hardening against real providers.

## Completed
- [x] Bootstrap, config (global/project TOML; secrets by env name), `trendlab` entry point, ruff
- [x] Event bus + redacted JSONL audit + SQLite persistence (schema v2: sessions, events, approvals,
      messages, session_state, model_calls, checkpoints)
- [x] Permissions: categories, risk, shell classifier (command-position network detection), policy
      per mode, session rules, project rules (`.trendlab/permissions.toml`), path/symlink boundary
- [x] Remote Approval System: ApprovalManager (fingerprint binding, tokens, replay/expiry/supersede,
      restart recovery), local + web + Textual channels, mobile page with diff preview, Telegram/ntfy/
      webhook notifications, expiry reminders, completion/failure notifications, questions (`ask_user`)
- [x] Providers: normalized errors, SSE streaming, capabilities + LOCAL/REMOTE label, model registry,
      structured-JSON tool fallback, gateway retry/backoff/fallback, role routing, escalation
- [x] UNSAFE mode is the default (no prompts): red badge, hard boundaries kept, destructive still asks,
      audited auto-approvals, mandatory pre-edit checkpoint; `--safe` / `/mode ask` turn prompts on
- [x] apply_patch unified-diff editor; secret scanning on writes; Anthropic prompt caching;
      compaction churn guard; redaction fix for token counters (2026-10-05)
- [x] Neon-on-black theme for TUI + REPL; TUI live streaming pane, spinner status, F-keys;
      /resume in place, /export, trendlab init, trendlab doctor (2026-10-05)
- [x] Live on DeepSeek Flash: benchmarks A–E green, long sessions with compaction green (2026-10-05)
- [x] Anthropic provider (official SDK): content-block translation, same-model thinking-block replay,
      adaptive thinking, effort, server-side refusal fallbacks, cache-aware usage; secrets store +
      `trendlab secret set` (2026-10-05)
- [x] Cost tracking (config pricing), budgets, `/cost`, `/cost-limit`
- [x] Tools: read/list/glob/search (ripgrep when present, ignore rules), write/patch (atomic, hash
      guard, diff), delete, shell, git_status/diff/log, run_tests (auto-detected), task, delegate, ask_user
- [x] Agent loop: structured plan, completion evaluator, loop detection + escalation, failure
      classification/recovery, limits (iterations/cost/calls/wall-clock), cancellation, final report
- [x] Context engine: repository map (cached), token budgeting, structured compaction (model or
      deterministic), overflow → compaction → retry
- [x] Sessions: `--resume latest|<id>`, `/sessions`, `/new`; checkpoints + `/undo` with conflict guard
- [x] Sub-agents: explorer/debugger/tester/reviewer, isolated context, read-only registry, parallel
      research, cost roll-up, `/review`
- [x] Ecosystem: hooks (blocking before_*), skills (`SKILL.md`), MCP stdio client, `/init`, first-run hints
- [x] UI: Textual TUI (default) with approval/question modal, Rich REPL (`--plain`), headless
      `-p --output json`, `trendlab sessions|approvals|bench`
- [x] Benchmarks A–E + runner (`trendlab bench -m provider:model`)
- [x] Security regressions: prompt injection, destructive commands, external paths

## Tests
133 test functions (≈170 cases with parametrization), all passing (`.venv/bin/python -m pytest -q`,
~30 s). `ruff check` and `ruff format --check` clean. Source: ~10k lines in `trendlab/`.

## Known Issues
- Only exercised against scripted/mocked providers in CI; real OpenAI/DeepSeek/Ollama runs still
  need a live shakedown (streaming edge cases, tool-call quirks).
- `/resume <id>` inside a running session only prints the command to restart with; use `--resume`.
- Sub-agents are read-only by design (no `write_access` yet).
- Patch tool is search/replace based (no unified-diff application).
- One harmless pytest warning: asyncio subprocess transport finalizer after loop close.

## Next Action
Live shakedown with a real provider on the benchmark fixtures (`trendlab bench -m deepseek:deepseek-chat`),
then tune prompts/evaluator thresholds from the results.
