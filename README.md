# TrendLab CLI

**TrendLab CLI** is a local-first, model-agnostic agentic coding harness: a terminal agent that
inspects a repository, edits files, runs commands and tests, and keeps working until there is
evidence the task is done — with every risky operation gated by a permission engine you control.

Its headline capability is the **Remote Approval System**: start a long task on your laptop,
walk away, and approve or deny permission requests from your phone. TrendLab resumes the moment
you decide.

```text
⏳ Waiting for approval
   Action:   Install Python package
   Command:  pip install pandas-ta
   Risk:     MEDIUM
Remote approval request sent — decide here or on your phone.
```

## Install

```bash
git clone <this repo> && cd trendlab-cli
python3 -m venv --without-pip .venv            # or a normal venv if ensurepip is available
pip3 --python .venv/bin/python install -e ".[dev]"
.venv/bin/trendlab --version
```

Configuration lives in `~/.trendlab/config.toml` (global) and `.trendlab/config.toml`
(per project). Secrets are environment variables only (`OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, …).
See `docs/config.example.toml`. Project instructions go in `TRENDLAB.md`.

## Use

```bash
trendlab                              # interactive REPL in the current project
trendlab -m deepseek:deepseek-chat    # pick a provider:model
trendlab -p "Run the tests and fix failures" --mode auto_edit
```

Slash commands: `/help /status /mode /permissions /remote /approvals /model /clear /quit`.

## Remote approval from your phone

**On the laptop (one time):**

```bash
# Bind to an address your phone can reach. Tailscale is the recommended transport:
trendlab remote enable --host 100.x.y.z --allow-insecure-http --machine-name ThinkPad
# (or give --public-url / tls_cert + tls_key in config for HTTPS)
trendlab remote url        # prints the pairing link — open it ONCE on your phone
```

Optional phone notifications (pick one provider in `[notifications]`):

```toml
[notifications]
enabled = true
provider = "ntfy"          # or "telegram" / "webhook"
[notifications.ntfy]
topic = "trendlab-<random>"
```

**On the phone:** open the pairing link once (the token is stored in the browser), then open
the same URL whenever a notification arrives. Each pending request shows machine, project,
action, command, risk, directory and expiry with **Approve Once / Approve for Session / Deny**.

**Security model (short version):** bearer-token API, per-request decision tokens, SHA-256
operation fingerprint binding, single-use decisions, expiry (default 30 min), high-risk
operations terminal-only by default, redaction of secrets everywhere, full audit trail
(`~/.trendlab/logs/events.jsonl` + SQLite). A restarted TrendLab cancels stale pending approvals
instead of executing them. Details: `docs/TRENDLAB_CLI_SPEC.md` §17, §35, §36.

## Develop

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Layout follows the spec (`docs/TRENDLAB_CLI_SPEC.md` §9): `config/ permissions/ approvals/
telemetry/ sessions/ tools/ providers/ agent/ ui/ security/`. Build history: `BUILD_STATUS.md`.
