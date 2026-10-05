# TrendLab CLI

**TrendLab CLI** is a local-first, model-agnostic agentic coding harness: a terminal agent that
inspects a repository, plans, edits files, runs commands and tests, recovers from failures, and
stops only when there is evidence the task is done. Every risky operation passes through a
permission engine you control, and you can approve, deny or answer questions **from your phone**.

```text
⏳ Waiting for approval
   Action:   Patch src/orders.py (+3 -1)
   Risk:     MEDIUM
Remote approval request sent — decide here or on your phone.
```

## What it does

- **Any model.** Anthropic (Claude Opus 5, Sonnet 5, Haiku 4.5 via the official SDK), OpenAI,
  DeepSeek, Moonshot/Kimi, Ollama or any OpenAI-compatible endpoint behind one gateway with retries, fallback, streaming, cost tracking and a JSON fallback for models
  with weak native tool calling. Hot-switch with `/model`; route roles with `[routing]`.
- **Real tools.** read/list/glob/search, atomic `write_file` and targeted `patch_file` with diff
  previews and hash conflict protection, `shell` with risk classification, `run_tests` with
  auto-detected validation commands, read-only git tools, `task` plan, `delegate` sub-agents,
  `ask_user`, and MCP servers as first-class tools.
- **Evidence-based completion.** A structured plan, a completion evaluator that refuses "done"
  without validation, loop detection with model escalation, iteration/cost/time limits,
  automatic checkpoints and `/undo`.
- **Long sessions.** Repository map, token budgeting, structured compaction, SQLite persistence
  and `--resume`.
- **Remote approval.** Authenticated mobile web page, Telegram/ntfy/webhook notifications,
  expiry reminders, completion/failure pings, questions answered from the phone, full audit trail.
- **Two interfaces, one look.** A full-screen TUI (default) and a Rich REPL (`--plain`), both
  jet-black with neon-green text: live token streaming, plan panel, spinner status bar with
  context and cost, inline diffs, and approval/question modals. Headless `-p "..." --output json`
  for CI.
- **Edits that land.** Exact-text `patch_file` for small changes, unified-diff `apply_patch` for
  multi-hunk, multi-file changes (all-or-nothing), and a secret scanner that refuses to write
  keys into the repository.

## Install

```bash
git clone <this repo> && cd trendlab-cli
python3 -m venv --without-pip .venv            # or a normal venv if ensurepip is available
pip3 --python .venv/bin/python install -e ".[dev]"
.venv/bin/trendlab --version
```

Configuration: `~/.trendlab/config.toml` (global) and `.trendlab/config.toml` (per project).
Secrets are referenced by name only (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, …) and resolved from
the environment or from `trendlab secret set NAME` (stored under `~/.trendlab/secrets`, mode 0600,
never echoed). Start from `docs/config.example.toml`. Project instructions live in `TRENDLAB.md` (`/init` drafts one).

## Use

```bash
trendlab                                  # full-screen TUI in the current project
trendlab --plain                          # Rich REPL
trendlab -m anthropic:claude-opus-5       # pick provider:model
trendlab -m deepseek:deepseek-chat
trendlab -p "Run the tests and fix failures" --auto-edit --output json   # headless / CI
trendlab --safe                           # approval prompts on (default is UNSAFE: no prompts)
trendlab --allow-destructive              # unsafe + rm -rf / destructive git without asking
trendlab --resume latest                  # continue the last session for this project
trendlab init                             # first-run wizard: provider, key, config
trendlab doctor                           # check config, keys, tools, remote settings
trendlab sessions | trendlab approvals    # history
trendlab bench -m ollama:qwen3-coder      # benchmark fixtures A–E on a model
```

**Permissions default to UNSAFE**: edits, shell, installs, network and deletes run without prompts;
`sudo` and paths outside the project are always denied, destructive commands still ask, every
auto-approval is logged, and `/undo` restores the pre-edit checkpoint. `--safe`, `/mode ask`, or
`permission_mode = "ask"` in config turn prompts on.

Slash commands: `/help /status /mode /permissions /plan /context /compact /cost /cost-limit
/diff /git /commit /checkpoint /undo /sessions /resume /new /export /model /models /review /init
/skills /hooks /mcp /remote /approvals /clear /quit`. TUI keys: F1 help, F2 plan, F3 cost,
Ctrl+L clear, Ctrl+C cancel (twice to quit).

## Remote approval from your phone

**On the laptop (one time):**

```bash
# Bind to an address your phone can reach. Tailscale is the recommended transport:
trendlab remote enable --host 100.x.y.z --allow-insecure-http --machine-name ThinkPad
# (or set tls_cert + tls_key / public_url in config for HTTPS)
trendlab remote url        # prints the pairing link — open it ONCE on your phone
```

Optional notifications (pick one provider in `[notifications]`; see `docs/config.example.toml`):

```toml
[notifications]
enabled = true
provider = "ntfy"          # or "telegram" / "webhook"
notify_on = ["approval", "question", "completion", "failure"]
[notifications.ntfy]
topic = "trendlab-<random>"
```

**On the phone:** open the pairing link once (the token is stored in the browser), then open the
same URL whenever a notification arrives. Each card shows machine, project, action, command,
risk, directory, expiry and the proposed diff, with **Approve Once / Approve for Session / Deny**.
Questions from the agent appear as cards with option buttons or a text box.

**Security model (short version):** bearer-token API with lockout, per-request decision tokens,
SHA-256 operation fingerprint binding, single-use decisions, expiry (default 30 min), high-risk
operations terminal-only by default, project-scope rules terminal-only, redaction of secrets
everywhere, full audit trail (`~/.trendlab/logs/events.jsonl` + SQLite). A restarted TrendLab
cancels stale pending approvals instead of executing them. Details: `docs/TRENDLAB_CLI_SPEC.md`
§17, §35, §36.

## Develop

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Layout follows the spec (`docs/TRENDLAB_CLI_SPEC.md` §9): `config/ permissions/ approvals/
telemetry/ sessions/ tools/ providers/ agent/ context/ orchestration/ extensions/ benchmarks/
ui/ security/`. Build history: `BUILD_STATUS.md`.
