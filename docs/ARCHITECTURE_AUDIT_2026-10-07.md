# TrendLab CLI — Adversarial Architecture Audit (2026-10-07)

Benchmark: the ten pillars of the LangSmith Engine architecture as compiled in
`harness-key-phrases-strategy.md` (120 key phrases, Max Agency podcast, Ben Tannehill).
Reference products: Claude Code and Codex CLI. Codebase under review: `trendlab-cli` at commit
`dddec0b` (spec v1.6, 304 tests). Citations are file paths in this repository.

Scoring: 1–3 absent · 4–6 basic or heuristic · 7–8 solid, matches Claude Code / Codex ·
9–10 production-grade, exceeds them through the Engine principles.

---

## 1. Executive scorecard

| Pillar | Architectural pillar | Score | Status | Primary gap |
|---|---|---|---|---|
| 1 | Core harness architecture & sandbox isolation | 6 | Partial | Orchestrator and tools share one process on the developer's machine; bubblewrap isolates *commands*, not the harness; Linux-only. |
| 2 | Context engineering & progressive ingestion | 5 | Partial | Hard truncation (20 k chars) and compaction exist, but no tiered ingestion: a 2 000-line test failure lands in the lead model's prompt as head+tail text. |
| 3 | Multi-agent delegation, "org chart" | 6 | Partial | Four read-only sub-agent roles with isolated context exist, but delegation is optional and model-initiated; raw tool output still reaches the lead. |
| 4 | Workflow vs autonomy | 8 | Implemented | Atomic tools in a plain loop with heuristic guards; little procedural scaffolding. Some guards are rules that should be skills. |
| 5 | Dynamic tooling & skillification | 5 | Partial | Skills load only by hand (`/skills use`); strong tool-call middleware (invalid-args feedback, rescue, path translation); no skill proposals from failures. |
| 6 | Ambient execution, alert fatigue, inbox | 4 | Partial | Rich human-in-the-loop (phone page, Telegram buttons, remote control) but no scheduler, no daemon, no triage inbox, no clustering, no "dismiss and don't remind". |
| 7 | Memory architecture & sleeptime compute | 7 | Implemented | Instruction files injected every session; per-run background fact extraction into `.trendlab/memory.md`; no overnight consolidation, no PR-reviewed memory. |
| 8 | Cost engineering & model cocktail | 7 | Implemented | Model-agnostic in practice, role routing table, per-call cost with role in SQLite, 87 % cache hits measured. Attribution is per role, not per tool; no automated hill-climb. |
| 9 | Evaluation harnesses & synthetic stubs | 6 | Partial | 304 offline tests with stubbed providers, GitHub CLI and Telegram; 5 synthetic bug fixtures with "unnecessary change" detection. No 50-task IssueBench, no containerised runs, no shadow replay. |
| 10 | Automated verification & self-improving meta-loop | 6 | Partial | Evidence-based completion is real and unusual. But edits land in the working tree before verification, no regression-test requirement, and the harness never reads its own traces. |
| **Overall** | **Average 6.0 / 10** | | | **Competitive readiness: Par** with Claude Code and Codex overall — ahead on 4, 7, 8 and on human-in-the-loop breadth; behind on 2, 3, 6 and 10, which are exactly the pillars that define the Engine approach. |

A note on the benchmark itself. Claude Code and Codex would score roughly 5 on pillars 1, 2, 3, 6 and 10 by the same rubric: they too run in the developer's shell, dump tool output into one context, use one model, and surface unverified diffs. "Par" here means TrendLab shares their architecture on those pillars, not that it is as polished. On pillar 8 it is clearly ahead of both; on pillar 7 it is ahead of Codex and level with Claude Code.

---

## 2. Pillar-by-pillar critique

### Pillar 1 — Core harness architecture & sandbox isolation

**Current state.** `TrendLabApp` (`trendlab/app.py`) runs the orchestrator, the tool runtime, SQLite persistence and the UI in one Python process on the developer's machine. Shell commands and background processes run under bubblewrap when it is installed (`trendlab/security/sandbox.py`: read-only root, project read-write, private `/tmp`, PID/IPC/UTS namespaces, network off unless the command is classified as network or package work). Tool subprocesses have timeouts (`ShellInput.timeout`), are killed on cancellation, and background processes die with the session (`trendlab/tools/background.py`). Output is capped at 20 000 characters head+tail (`MAX_CAPTURE`) and redacted. State survives restarts: sessions, messages, approvals and checkpoints live in SQLite; `ApprovalManager.recover()` cancels approvals left pending by a dead process (drilled live, `scripts/failure_drills.py`).

**Gap and vulnerability analysis.**
- The harness is *inside* the environment. A hostile command cannot escape bubblewrap, but it is still launched by, and streams into, the same process that holds the session state. A Python-level fault in a tool (an exception is caught; a segfault in a C extension is not) takes the orchestrator with it.
- bubblewrap is Linux/WSL only. On macOS and native Windows every command runs with the developer's full privileges; `trendlab doctor` warns but nothing compensates.
- Malicious output is treated as data by prompt rules (`trendlab/agent/prompt.py` `_POLICY`), secret-scanned on writes and redacted on the way in, but there is no structural barrier: a tool output can still carry instructions into the lead model's context. This is the same exposure Claude Code and Codex have.
- An infinite loop inside a command is bounded by the per-call timeout; an infinite loop *in the model* is bounded by iteration, cost and wall-clock limits (`LimitsConfig`). Both are heuristics rather than isolation.
- There is no durable daemon. Close the terminal and the session ends; nothing keeps running between sessions.

**Rating: 6.** Solid command-level isolation on Linux, state that survives crashes, no process-level or host-level separation.

**Directives for 10/10.**
1. Split into `trendlab-engine` (durable daemon: sessions, approvals, memory, scheduler, event bus over a local socket) and `trendlab` (thin client: TUI/REPL/Telegram). Model: Codex's app-server and Claude Code's remote control, but local-first.
2. Add a `Sandbox` interface with three drivers: `bwrap` (today), `docker` (ephemeral container per session with the project bind-mounted), and `remote` (SSH to a throwaway VM). Select by config and platform; macOS gets `docker` by default.
3. Run every tool call through the sandbox driver as an RPC, so a tool process crash is a tool error, never an orchestrator fault. Record the driver in `tool.started` events.
4. Treat tool output as untrusted at the type level: a `ToolOutput` object carrying provenance that the context builder wraps in a fixed delimiter block, and a middleware hook that strips instruction-like lines ("ignore previous", "run the following") into a separate audited field rather than the prompt.

### Pillar 2 — Context engineering & progressive telemetry ingestion

**Current state.** The system prompt is small and stable within a run (plan rendered once per run for cache hits, `trendlab/agent/runtime.py`). Repository map is budgeted (`ContextConfig.repo_map_budget_tokens`), history is budgeted and compacted into a structured summary (`trendlab/context/manager.py`, `compaction.py`), overflow triggers compaction then retry. Tool outputs are capped at 20 000 characters head+tail (`trendlab/tools/shell.py::_truncate`), `read_file` takes line ranges, `search_text` and `glob` cap results, `web_fetch` is size-capped. `tool.completed` events carry a two-line preview for the human. `@file` attachments are capped per file and per prompt. Streaming tool output is transient and never persisted.

**Gap and vulnerability analysis.**
- There is no *tiering*. A failing test run returns up to 20 k characters of head and tail into the lead model's context in one tool message. The Engine model would return Tier 1 first ("pytest: 2 failed, 118 passed, 41 s, exit 1"), Tier 2 on request (the two failing test names with their assertion lines), Tier 3 only to a worker. TrendLab's head+tail truncation is the worst of both: the model sees the setup noise and the summary line, and loses the middle where the traceback usually is.
- Diagnostics after edits are appended raw to the tool result (`trendlab/tools/runtime.py`); a noisy linter can add thousands of characters per edit.
- No per-tool token budget: the cap is characters, uniform across tools, and not configurable per tool or per model window.
- No baselines. The harness does not know that this project's test suite normally takes 40 s and 0 failures, so it cannot flag "this run took 9 minutes" as an anomaly (key phrase 19).
- `list_directory` on a 4 000-entry folder returned 500 lines to the model during live use (session 45be7282480d); the model then hallucinated. A stats tier ("4 029 entries, 10 modified today, extensions: pdf 3 900, lock 2") would have answered the question outright.

**Rating: 5.** Budgets and compaction are real; progressive ingestion is absent.

**Directives for 10/10.**
1. Introduce `ToolOutput(tier1: str, tier2: str | None, tier3_ref: Path)` as the return type of every tool that can produce large output (`shell`, `run_tests`, `search_text`, `list_directory`, `read_file` over N lines, `web_fetch`). Tier 1 is always ≤ 400 characters of facts; Tier 3 is written to `.trendlab/traces/<call_id>.log` on disk.
2. Add `inspect_output(call_id, query | line_range)` as a tool so the lead model pulls Tier 2/3 slices on demand, and route calls over 8 k tokens of Tier 3 through a screener sub-agent automatically (see Pillar 3).
3. Per-tool, per-model token budgets in config (`[context.tool_budgets] shell = 2000, run_tests = 1500`), enforced by summarisation (cheap model) rather than by cutting the middle out.
4. Test-output parsers for pytest, jest, cargo, go test and ruff that produce Tier 1/2 deterministically (failing test names, first assertion line, file:line) before any model sees them.
5. Baselines in SQLite per project and command: median duration and exit code; emit `anomaly.detected` when a run exceeds 3× baseline and surface it as a Tier 1 fact.

### Pillar 3 — Multi-agent delegation & the "org chart"

**Current state.** `trendlab/orchestration/subagents.py` defines explorer, debugger, tester and reviewer roles with isolated context, a read-only tool registry (plus `run_tests`), a per-task token budget (40 k) and iteration cap, a role-routed model (`routing.<role>`), and cost roll-up into the parent. The lead model invokes them through the `delegate` tool and receives a distilled `SubAgentReport`; `/review` runs the reviewer on the current diff. Parallel research is supported. Summarisation is a separate cheap role.

**Gap and vulnerability analysis.**
- Delegation is a suggestion, not a structure. Nothing forces large outputs through a screener; the lead model runs `shell` itself and absorbs the output (Pillar 2). In live sessions the `delegate` tool was rarely chosen by DeepSeek Flash.
- There is no verifier step by default. The reviewer exists but is opt-in via `/review`; a run can finish with the lead's own say-so plus the evaluator's heuristics.
- No issue-drafter role: commit and PR messages are generated by the lead model from the diff (`trendlab/orchestration/gitflow.py`), coupling diagnosis and authoring.
- Sub-agents cannot write (`write_access` is reserved), so the "coder worker" tier of the org chart does not exist; all editing is done by the lead, which is the most expensive seat.
- Sub-agent prompts inherit the role prompt but not a minimal, task-specific system prompt (key phrase 57); they are smaller than the lead's prompt but not minimal.

**Rating: 6.** The roles and isolation exist; the chart is drawn but the lead still does the heavy lifting itself.

**Directives for 10/10.**
1. Make screening structural: a `screener` role (cheapest configured model, local if present) that every Tier 3 output over a threshold passes through, returning Tier 1/2 to the lead. The lead never sees Tier 3 unless it asks `inspect_output`.
2. Make verification structural: a `verifier` role runs after the evaluator accepts and before the run is marked complete — a fresh context, the diff, the test output, and one question: "does this change do what was asked without collateral?" Findings return to the lead for one fix round. Config `verification = "required" | "advisory" | "off"`.
3. Add an `author` worker with write access confined to a git worktree (Pillar 10) so the lead plans and reviews while a cheaper code-tuned model edits.
4. Separate `issue_drafter`: structured diagnosis card (problem, root cause, impacted files, evidence, proposed diff) produced by a cheap model from the trace, used by `/pr`, the inbox (Pillar 6) and Telegram reports.
5. Per-role minimal prompts: screener and verifier get under 300 tokens of instructions and no repository map.

### Pillar 4 — Workflow vs autonomy

**Current state.** The agent loop (`trendlab/agent/runtime.py`) is a plain observe-act loop over atomic tools (`read_file`, `search_text`, `shell`, `patch_file`, `apply_patch`, `run_tests`, `git_*`, `web_*`, `task`, `delegate`, `ask_user`, `background_process`). There is no DAG; the model chooses tool order. Constraints are expressed as instructions (`_POLICY`, project instruction files, project memory) and a handful of heuristic guards: the completion evaluator, loop detector, re-plan guard, tool-call rescue, empty-answer nudge. Plans are optional and model-maintained. Workflows that are deterministic are atomic by design: commit, PR, worktree creation, diagnostics after edit.

**Gap and vulnerability analysis.**
- The weak-model guards (rescue of tool calls written as text, re-plan guard, "announced but not called" nudge) are procedural rules added one failure at a time during local-model sessions. They are small, but they are exactly the scaffolding key phrase 49 says to delete as models improve; with Flash or Claude they never fire and only add code.
- The plan gate and the evaluator nudges are fixed-text instructions injected by code, not by skill files the user can edit (Pillar 5).
- No audit mechanism for unnecessary determinism: nothing measures how often each guard fires per model, so there is no evidence to retire any.

**Rating: 8.** The architecture is on the right side of the line; the guards need to be measured and made editable.

**Directives for 10/10.**
1. Emit `guard.fired {guard, model}` for every heuristic intervention and show counts per model in `/cost` and the benchmark report; retire or model-gate guards that never fire on frontier models.
2. Move nudge texts and the plan-gate text into skill-style markdown under `trendlab/prompts/` so they are tunable without code changes (key phrase 64).
3. Keep the tool surface orthogonal: fold `run_tests` into `shell` with a `kind` hint rather than a separate tool, and document the invariant "one tool = one side-effect class".

### Pillar 5 — Dynamic tooling & skillification

**Current state.** Skills exist as `SKILL.md` packs under `~/.trendlab/skills/` and `.trendlab/skills/` (`trendlab/extensions/skills.py`) with optional `skill.toml` metadata, activated by `/skills use <name>` and appended to the system prompt. Custom slash commands from Markdown (`trendlab/extensions/commands.py`). Middleware in the sense of key phrases 61–62 is strong: invalid arguments return a structured "needs X, given Y" message and are visible as `tool.skipped`; tool calls written as text are rescued; Windows paths are translated; `~` is expanded; irreversible commands are classified from the command line. Tools are also CLI-runnable by humans (`trendlab bench`, `trendlab doctor`, `trendlab keys`, the failure drills).

**Gap and vulnerability analysis.**
- Skills never load themselves. No trigger by file type, path, technology or failure. The developer must know a skill exists and activate it, so in practice skills are unused.
- The agent cannot create or amend skills; project memory stores facts, not procedures (spec §84 lists "learned skills" as future work).
- No per-driver prompt tuning (key phrase 60): the same tool schemas and policy text go to Anthropic, OpenAI-compatible, DeepSeek and local models, even though the local-model failures showed they need different instructions.
- The core system prompt is small, but there is no measurement of its token size or of skill retrieval precision (key phrase 63).

**Rating: 5.** Good middleware, dormant skills.

**Directives for 10/10.**
1. Skill triggers in `skill.toml`: `paths = ["**/*.tsx"]`, `tools = ["docker"]`, `on_failure = ["ModuleNotFoundError"]`, `keywords = [...]`. The context builder loads matching skills when a touched file, a failing output or the prompt matches, and unloads them when the run moves on. Log `skill.loaded` with the trigger.
2. `learned skills`: after a run the evaluator accepted with ≥ 5 tool calls, a cheap model drafts `.trendlab/skills/<slug>/SKILL.md` (steps, commands, pitfalls); the TUI offers `[K]eep` / discard; kept skills gain triggers from the files touched.
3. Skill repair: when a tool call fails twice with the same error while a skill is loaded, ask the summarizer to propose a one-line amendment to that skill and queue it in the inbox (Pillar 6).
4. Driver profiles: `trendlab/prompts/drivers/{anthropic,openai,deepseek,local}.md` merged into the policy text per provider type, and tool descriptions trimmed for small models.
5. Measure: a `prompt.built` event with token counts by section (policy, instructions, memory, skills, repo map) so skill loading can be benchmarked.

### Pillar 6 — Ambient execution, alert fatigue & the inbox

**Current state.** Human-in-the-loop is broad: an authenticated phone page with inline diff and single-use, fingerprint-bound decisions; Telegram inline-button approvals; Telegram remote control (prompts, steering, answers, read-only commands, reports back); questions from the agent delivered to every channel; expiry reminders; completion and failure notifications; a plan gate. Approvals are audited and recover across restarts.

**Gap and vulnerability analysis.**
- Nothing runs unless a person starts it. There is no scheduler, no daemon, no "watch the repo and triage failures" mode (key phrases 11, 12, 65). Spec §84 lists scheduled jobs and the issue-to-PR autopilot as next steps.
- Notifications are per event: one message per run completion, one per approval, one per question. For a developer running many short tasks from Telegram this is the "super noisy" failure mode (key phrase 68) in miniature; there is no clustering, no digest, no severity threshold.
- No inbox. Findings from `/review`, diagnostics and failed runs are printed into the transcript and lost when it scrolls. There is no persistent list of candidate issues with Apply / Verify / Dismiss, no frequency ("failed in 3 of the last 4 runs"), no "dismiss and don't remind" that writes to memory.
- Patches are applied directly to the working tree in auto mode; the staging step that key phrase 67 warns about does not exist, which is acceptable interactively but rules out ambient operation.

**Rating: 4.** Excellent interactive HITL plumbing with none of the ambient layer on top of it.

**Directives for 10/10.**
1. `trendlab watch` (daemon, Pillar 1): on commit, on a schedule, or on CI failure webhook, run the screener over test output and traces and write *findings*, never patches, to the inbox.
2. Inbox data model in SQLite: `issue(id, cluster_key, title, root_cause, impacted_files, evidence_refs, first_seen, last_seen, occurrences, severity, status, proposed_patch_ref, verification_ref)`. Clustering by normalised stack-frame signature and failing-test name.
3. `/inbox` TUI screen and Telegram digest: one message per cycle with counts and the top three cards; actions `[A]pply` (in a worktree, verified before merge, Pillar 10), `[T]est`, `[D]ismiss`, `[S]ilence` (writes a rule to project memory: "ignore flake8 E501 in legacy/").
4. Severity thresholds in config and per-issue natural-language feedback on dismiss ("Why? Enter to skip") appended to memory (key phrase 85).
5. Collapse per-run Telegram messages into a digest when more than N complete within a window; keep approvals and questions immediate.

### Pillar 7 — Memory architecture & sleeptime compute

**Current state.** `TRENDLAB.md`, `AGENTS.md` and `CLAUDE.md` are injected into every session's system prompt (`trendlab/agent/prompt.py::project_instructions`). Project memory (`trendlab/context/project_memory.py`) stores one dated fact per line in `.trendlab/memory.md`, capped and deduplicated by normalised text, loaded within a 6 000-character budget newest-first, and extended after noteworthy runs by the summarizer role in a background task with a 90-second cap, so the interactive session is never blocked (key phrase 86). The deterministic fallback keeps passing validation commands and steering lines. `/memory`, `/memory forget`, `/remember`. Compaction summaries carry structured state across a session.

**Gap and vulnerability analysis.**
- Learning is per run, not sleeptime. There is no overnight pass over the day's sessions, commits and corrections that consolidates, prunes and rewrites the memory file (key phrases 83–88). The cap prevents bloat but not drift: contradictory or stale facts coexist until a human prunes them.
- Memory updates are written directly, not proposed. Key phrase 89's model, memory changes as reviewable pull requests against `AGENTS.md` and skill files, does not exist; `.trendlab/` is git-excluded, so memory is not versioned with the code at all.
- Corrections are captured only as steering text during a run. Denials, rejected plans, `/undo` and Telegram answers are not mined for rules.
- No frustration or repeated-rejection detector (key phrase 23).

**Rating: 7.** Level with Claude Code's instruction files plus a genuine, non-blocking learning loop; short of the consolidation and review model.

**Directives for 10/10.**
1. `trendlab sleep` (and a scheduled hook in the daemon): read the day's sessions from SQLite (messages, denials, undos, steering, dismissed inbox items, memory additions), consolidate `.trendlab/memory.md` into ≤ 40 facts, draft amendments to `TRENDLAB.md` and skill files, and open a branch `trendlab/memory-YYYY-MM-DD` with the diff; the developer merges it. Report the summary to Telegram.
2. Track memory in git by default (`.trendlab/memory.md` and `.trendlab/skills/` un-excluded), with the session database still excluded.
3. Mine corrections: every `approval.denied`, `plan.rejected`, `session.checkpoint.restored` and Telegram "no" becomes a candidate rule with the context that triggered it.
4. Frustration detector: three rejections or rephrasings in one session raise a `user.frustrated` event, pause automatic memory writes, and ask one question at the end of the run.

### Pillar 8 — Cost engineering & the model cocktail

**Current state.** Provider-neutral by construction: Anthropic (official SDK with prompt caching), any OpenAI-compatible endpoint (DeepSeek, OpenAI, Moonshot), native Ollama with the model's real context window. Role routing table (`routing.default|planning|explorer|debugger|tester|reviewer|summarizer|escalation`) in `trendlab/providers/registry.py`; escalation is per run with restore; vision prompts route to the cheapest vision-capable model with a key. Every model call is recorded with model, role, tokens, cached tokens, latency and cost (`sessions.model_calls`), priced from config with cached-input rates; `/cost` shows totals by model; budgets stop runs. Measured: 87 % cached input tokens on DeepSeek over 77 calls, $0.077. The model picker shows price, context and key status; the benchmark runner reports cost per fixture per model.

**Gap and vulnerability analysis.**
- The cocktail is configured, not exercised: by default the lead model does everything, because screening is not structural (Pillar 3). The cheap seats exist in the routing table and sit empty.
- Attribution stops at the role. Cost is not attributed to tools, to phases (exploration vs editing vs validation), or to sub-agent tasks, so the "33 % of our cost" discovery (key phrase 95) cannot be made from the data.
- No hill-climbing: nothing re-runs the benchmark when a routing entry or prompt changes, and the benchmark has five fixtures.
- Local models for screening are supported but, after the owner's experience, discouraged; there is no graded policy such as "local model for file search, API model for reasoning".

**Rating: 7.** Ahead of both reference products on model freedom and cost visibility; the routing is a table waiting for structure.

**Directives for 10/10.**
1. Phase and tool attribution: tag each model call with the phase (plan, explore, edit, validate, verify, summarise) inferred from the tools used since the last call, and store `tool_calls_since` with it; `/cost --by phase|tool|role`.
2. Default cocktail: screener = cheapest configured model (local if it passes the screener benchmark), verifier = balanced, planner/author = session model, summarizer = cheapest; ship it as the example config and make `trendlab doctor` warn when every role resolves to the same model.
3. `trendlab bench --compare routing-a.toml routing-b.toml` to hill-climb routing and prompts on the fixture suite with cost and outcome deltas.
4. A screener benchmark (log in, expected Tier 1/2 out) so a local model can be admitted to the screener seat on evidence, not on parameter count.

### Pillar 9 — Evaluation harnesses & synthetic stubs

**Current state.** 304 offline tests: providers behind `httpx.MockTransport`, a scripted provider, a fake GitHub CLI, bare git remotes, a fake Telegram Bot API, Textual's headless pilot, bubblewrap integration tests that skip without `bwrap`. Five synthetic benchmark fixtures with seeded bugs and expected changed-file sets, reporting calls, time, cost, outcome and unnecessary changes (`trendlab/benchmarks/`). Live drills against the real provider for fallback and restart recovery (`scripts/failure_drills.py`) in a throwaway home. A 40-check UX audit with evidence per check.

**Gap and vulnerability analysis.**
- Scale: five fixtures versus IssueBench's ~50; no multi-file, needle-in-haystack diagnosis tasks (key phrase 105); no eval of discovery, diagnosis and verification as separate competencies (key phrase 104).
- No containerised, reproducible runs (Harbor); benchmarks run in the developer's environment with whatever Python and tools are present.
- No shadow replay: sessions are fully persisted (messages, events, tool calls) but nothing replays a past session against a new harness build to detect regressions in behaviour or verbosity (key phrases 115–116).
- Stubs cover the harness's own dependencies, not the *target project's* services: a repo under test that talks to a database or HTTP API gets no generic stub server from TrendLab.
- No regression gate on prompt or routing changes (ties to Pillar 8).

**Rating: 6.** Thorough unit and integration stubbing; thin end-to-end evaluation.

**Directives for 10/10.**
1. Grow the fixture suite to 50 tasks across Python, TypeScript and Go, generated by a seeding script that injects categorised bugs (off-by-one, wrong import, missing null check, race) into clean repos with passing tests, so labels are free (key phrases 108–109).
2. Split metrics by competency: located the right file, named the root cause, patch passes tests, no unnecessary changes, regression test added.
3. Run `trendlab bench` inside the sandbox driver (Docker when available) with pinned toolchains; publish results as a JSON artefact per commit.
4. Shadow replay: `trendlab replay <session-id>` re-runs a stored session's prompts against the current build with the same scripted tool results where possible and diffs the transcript, tool choices and cost.
5. A generic stub server (`trendlab stub --spec openapi.yaml`) the agent can start for projects whose tests hit external HTTP services, with the policy that `run_tests` in benchmark mode refuses non-loopback network.

### Pillar 10 — Automated verification & the self-improving meta-loop

**Current state.** Evidence-based completion (`trendlab/agent/evaluator.py`): a run cannot complete while files changed without a validation run, the last validation failed, plan items are open, or the answer is empty or a promise; two rejections escalate. Diagnostics run after every edit. `run_tests` results are recorded as validation evidence and shown in the footer and in `/pr` bodies. Checkpoints before every mutation; `/undo`. Everything the harness does is logged as redacted events in SQLite and JSONL. The failure drills and the UX audit are the beginnings of running the harness against itself.

**Gap and vulnerability analysis.**
- Edits are made in the working tree first and verified second. The Engine principle (key phrases 117–120) is verify-then-surface: apply in an isolated worktree, run the suite, and only then present a passing diff. TrendLab's evaluator refuses to *finish* without validation, but by then the files are already changed and a failing attempt leaves the tree dirty until `/undo`.
- No regression-test requirement: a bug fix can complete with the existing suite green and no new test (key phrase 123). The evaluator does not ask for one.
- No meta-loop. The audit log is complete and redacted, but nothing reads it: no job finds sessions that ended FAILED, clusters stop reasons, or proposes harness fixes. Every bug found this week was found by a human reading sessions by hand.
- Read-only versus write-access tools are categorised (Pillar 1), but there is no "run this agent read-only against a branch" eval mode (key phrase 121).

**Rating: 6.** The completion contract is better than either reference product; the verification order and the meta-loop are missing.

**Directives for 10/10.**
1. Verify-then-surface mode (`[verification] mode = "worktree"`): the author works in `git worktree add .trendlab/wt/<run>`; the suite runs there; on green the diff is applied to the working tree (or offered as a patch) with the test log attached; on red the lead gets Tier 2 of the failure and tries again, the main tree untouched. Interactive runs may opt out; ambient runs may not.
2. Regression-test gate: when a run's changed files include non-test source and the task reads as a fix, the evaluator requires a new or modified test that fails before and passes after (the author runs it both ways in the worktree) or an explicit, logged reason.
3. `trendlab meta` (and a daemon schedule): a screener reads the last N sessions' events, clusters failures by `stop_reason`, denied categories, loop reasons and `tool.skipped` reasons, and writes inbox cards against the *harness* with the session IDs as evidence; a second pass drafts fixes in a worktree of `trendlab-cli` itself with tests.
4. Read-only eval mode: `trendlab -p ... --dry-run` runs the full loop with write tools mocked (diff captured, nothing applied), for safe evaluation of write-access behaviour.

---

## 3. Top three highest-leverage upgrades

**1. Progressive ingestion through a structural screener (Pillars 2, 3, 8).** Give every large-output tool a `ToolOutput` with Tier 1 facts, on-disk Tier 3, and an `inspect_output` tool; route Tier 3 over a threshold through a cheap screener sub-agent automatically, and add deterministic parsers for pytest, jest, cargo, go and ruff. This is the single change that attacks the quadratic context-degradation problem the Engine team describes, fills the empty cheap seats in the routing table, and would have prevented the 4 000-entry listing hallucination seen in live use. Estimated effort: two days. Expected effect: lead-model input tokens down by half or more on test-heavy tasks, higher accuracy on large failures.

**2. Verify-then-surface in a worktree, with a verifier role and a regression-test gate (Pillars 10, 3).** Author edits land in an isolated worktree, the suite runs there, a separate verifier model reads the diff and the test log, and only a passing, verified diff reaches the user's tree, with the log attached. This is the exact differentiator the strategy document names ("proposes a fix and then proves it works") and neither Claude Code nor Codex does it. It also unlocks ambient operation, because unverified patches never touch a tree a human is working in. Estimated effort: two to three days; most pieces exist (worktree manager, evaluator, reviewer role, checkpoints).

**3. The daemon with an inbox and a sleeptime pass (Pillars 1, 6, 7, 10).** `trendlab-engine` as a durable local service: `watch` runs the screener on commits, schedules and CI failures and writes clustered findings to an inbox with Apply / Test / Dismiss / Silence; `sleep` consolidates memory and proposes `TRENDLAB.md` and skill amendments as a reviewable branch; `meta` reads the harness's own sessions and files inbox cards against itself. This converts the strongest existing asset, the human-in-the-loop channels, from interruptions into a triage queue, and it is the architecture that lets TrendLab work while the developer is not. Estimated effort: four to five days, the daemon split being most of it.

Done in that order, pillars 2, 3, 6, 8 and 10 move from 4–6 to 8–9, the average passes 8, and the comparison against Claude Code and Codex changes from "par with a different safety story" to "a different architecture": external orchestration, hierarchical models, verified patches, and an inbox instead of a prompt flood. Every number in this audit is checkable against the cited files and the 304 tests; the directives above are the implementation record the next version of the spec should carry.
