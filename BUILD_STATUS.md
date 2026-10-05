# TrendLab Build Status

## Current Milestone
Remote Approval System — implemented on a minimal runtime foundation (2026-10-04).

## Completed
- [x] Bootstrap (pyproject, package layout, `trendlab` entry point, ruff)
- [x] Config (global + project TOML, env secrets by name, `[remote_approval]`, `[notifications]`)
- [x] Event bus + JSONL audit sink + SQLite event persistence (redacted)
- [x] Permissions: categories, risk levels, shell classifier, mode policy table, session rules
- [x] Approval Manager (fingerprint binding, tokens, replay/expiry/supersede, restart recovery)
- [x] Approval channels: local terminal, remote web (mobile page, bearer auth, lockout, TLS option)
- [x] Notification providers: telegram, ntfy, webhook (behind one abstraction, failure-tolerant)
- [x] Tools: read_file, list_directory, search_text, write_file (atomic + hash guard), delete_file, shell
- [x] Tool runtime: validate → permission → approval → binding re-check → execute → events
- [x] Providers: normalized contract, OpenAI-compatible (OpenAI/DeepSeek/Moonshot/Ollama /v1), scripted
- [x] Agent loop + explicit state machine (WAITING_PERMISSION)
- [x] Rich REPL with `/remote`, `/approvals`, `/permissions`, `/mode`, `/model`, `/status`
- [x] CLI: `trendlab`, `-p`, `remote status|enable|disable|url|rotate-token`, `approvals`
- [ ] Planner, completion evaluator, recovery, loop detection
- [ ] Context budgeting, compaction, repository map, checkpoints/undo, session resume
- [ ] Sub-agents, model routing, cost tracking
- [ ] Textual TUI, MCP, skills, hooks

## Tests
89 passed, 0 failed (`.venv/bin/python -m pytest -q`); `ruff check` and `ruff format --check` clean.

## Known Issues
- Streaming model output is not implemented (non-streaming chat completions only).
- Non-interactive `-p` mode with remote approval disabled cannot resolve ASK verdicts; they expire.
- No `--resume`; sessions and events are persisted but not yet reloaded into a new run.

## Next Action
Implement the planner + completion evaluator (Milestones 14/17) so long unattended runs that rely
on remote approval also stop on evidence rather than on the model saying "done".
