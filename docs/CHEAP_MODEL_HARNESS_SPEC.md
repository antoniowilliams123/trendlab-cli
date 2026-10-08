# TrendLab CLI — Cheap-Model-First Harness: Implementation Specification

**Status:** proposed, 2026-10-07 · to be merged into `TRENDLAB_CLI_SPEC.md` as §93 on delivery
**Owner's goal (verbatim intent):** a harness so good that even when it works with cheap models
such as DeepSeek Flash, the output is best in class — Claude Code / Codex calibre results at a
fraction of the cost.
**Inputs:** `ARCHITECTURE_AUDIT_2026-10-07.md` (ten-pillar audit, average 6.0), the LangSmith
Engine strategy (`harness-key-phrases-strategy.md`) and the full Max Agency transcript.

---

## 0. Thesis and the one metric that decides success

A cheap model fails in predictable ways: it loses the thread in large inputs, claims completion
without proof, makes plausible but wrong edits, derails on large tasks, varies run to run, forgets
conventions, and mangles tool calls. Every one of those is a *reliability* failure, and reliability
can be supplied by the harness: structured inputs, tests as the judge, isolated attempts, a second
opinion, retries chosen by evidence, and per-model guidance learned from the model's own traces.

What the harness cannot supply is judgement on design-heavy decisions. The Engine architecture
answers that with an org chart: the expensive model makes a few short calls (plan the split, verify
the result); the cheap models make the many calls in between. This spec adopts that shape.

**Success metric (M1).** On the 50-task fixture suite (§8), *DeepSeek Flash inside the full
harness* must match or beat *Claude Sonnet 5 with the harness stripped to plain tools* on:
completion rate, patch-passes-tests rate, no-unnecessary-changes rate, and regression-test-added
rate — at ≤ 10 % of the cost. M1 is measured by `trendlab bench --compare` (§8.4) and reported
in the spec on delivery. Secondary metrics: lead-model input tokens per task (target −50 % vs
today), human interventions per task (target 0 on the suite).

Non-goals: making Flash a better architect than Sonnet; cloud orchestration; replacing the
interactive TUI. The daemon (§7) is in scope only as far as the inbox and sleeptime pass need it.

---

## 1. Architecture after delivery

```
                       ┌────────────────────────────── trendlab-engine (daemon, §7) ───────┐
                       │  scheduler · inbox · sleeptime memory · meta-loop · event bus     │
                       └──────────────────────────────────────────────────────────────────┘
 interactive TUI / REPL / Telegram ──► TrendLabApp ──► AgentRuntime (lead: session model)
                                                        │
                   ┌────────────────────────────────────┼─────────────────────────────────┐
                   ▼                                    ▼                                 ▼
            planner call (routing.planning)      tools → ToolOutput (tiered, §2)    verifier (routing.verifier)
            2–3 calls per task, stronger model    Tier 3 on disk; screener (§3)       fresh context, diff + log
                   │                                    │                                 │
                   ▼                                    ▼                                 ▼
            step plan ──► author attempts in a git worktree (§4) ──► tests decide ──► surface or retry (§5)
```

Roles and default cocktail (§6): `lead` = session model (Flash); `planning` and `verifier` =
stronger model (DeepSeek V4 Pro, or Sonnet when keyed); `screener` and `summarizer` = cheapest
model with a key, local if it passes the screener benchmark; `author` = lead unless overridden.

---

## 2. Tiered tool output and agent-native views (audit pillar 2)

### 2.1 Data model
```python
class ToolOutput(BaseModel):
    tier1: str                 # ≤ 400 chars of facts; always present; what the lead sees first
    tier2: str | None = None   # ≤ 4 000 chars of targeted detail (failing tests, matches, headers)
    tier3_ref: str | None = None  # .trendlab/traces/<call_id>.log on disk; never auto-inserted
    stats: dict[str, Any] = {}    # exit_code, duration_ms, lines, bytes, counts by kind
    kind: str = "text"            # text | tests | lint | listing | search | diff | http
```
`ToolResult.output` becomes the rendered Tier 1 (+ Tier 2 when it fits the per-tool budget);
`ToolResult.data["tiers"]` carries the object. Existing tools keep working: a tool that returns
plain text is wrapped as Tier 1 = first 400 chars of a head+tail summary, Tier 3 = full text.

### 2.2 Deterministic parsers (`trendlab/tools/views/`)
No model reads raw output when a parser exists. Each parser produces Tier 1 and Tier 2:

| Kind | Parser | Tier 1 | Tier 2 |
|---|---|---|---|
| pytest / unittest | `tests_py.py` | `N passed, M failed, K errors, T s, exit E` | failing test ids, first assertion line, `file:line` |
| jest / vitest / mocha | `tests_js.py` | same | same |
| cargo test / go test | `tests_rs_go.py` | same | same |
| ruff / eslint / tsc / pyright / mypy | `lint.py` | `count by code, files touched` | first 30 findings grouped by file |
| `ls` / `list_directory` | `listing.py` | `entries, dirs, by extension, newest 5, size` | names filtered by the question's pattern |
| `search_text` / ripgrep | `search.py` | `matches, files` | top 40 matches with 1-line context |
| `git diff` / `git status` | `gitview.py` | files, +/- counts | per-file hunk headers |
| HTTP (`web_fetch`) | `http.py` | status, bytes, title | headings + matched paragraphs |

Parsers are pure functions over text and are unit-tested on captured fixtures.

### 2.3 `inspect_output` tool
```
inspect_output(call_id: str, query: str | None, lines: "start-end" | None, max_chars=4000)
```
Returns a Tier 2 or Tier 3 slice of a previous call's output. Grep-style `query` returns matching
lines with context. This is how the lead asks for more; it never gets more unasked.

### 2.4 Budgets
`[context.tool_budgets]` per tool in tokens (defaults: shell 1 500, run_tests 1 200, search_text
1 000, list_directory 600, read_file 2 500, web_fetch 1 500). Tier 2 is truncated to budget by
*summarisation through the screener* (§3) when a parser is absent, never by cutting the middle.

### 2.5 Baselines and anomalies
`baselines(project, command_key)` in SQLite: median duration, usual exit code, usual failing
count. `tool.completed` carries `anomaly: "3.4x slower than baseline"` when exceeded; it is part
of Tier 1.

### 2.6 Context assembly
System prompt unchanged (stable per run). Tool messages are Tier 1 (+2). Tier 3 never enters the
lead's context. Diagnostics after edits become a `lint` ToolOutput (Tier 1 counts, Tier 2 first
findings) instead of raw text.

**Events:** `tool.output_tiered {call_id, kind, t1_chars, t2_chars, t3_bytes, parser}`.
**Exit criteria:** every tool in `default_registry()` returns `ToolOutput`; parsers cover the eight
kinds; lead input tokens on fixtures A–E drop ≥ 40 % with unchanged outcomes.

---

## 3. Structural screener and verifier (pillar 3)

### 3.1 Screener
A sub-agent role with a ≤ 300-token prompt, no repository map, the cheapest configured model.
Invoked automatically by the tool runtime when a `ToolOutput` has no parser and Tier 3 exceeds
`screener.threshold_tokens` (default 3 000): input = the question the lead asked (the tool call's
`explanation`) + Tier 3; output = Tier 1/2 in the same schema. Also invoked by `inspect_output`
with a `query` when the slice exceeds budget. Cost and latency attributed to role `screener`.

### 3.2 Verifier (required)
Runs after the completion evaluator accepts and before the run is marked COMPLETED, when files
changed. Fresh context: task text, the diff, the latest validation ToolOutput (Tier 1+2), the plan.
One question with a fixed schema:
```json
{"verdict": "pass" | "fix" | "fail", "findings": [{"file": "...", "line": 0, "issue": "...", "severity": "high|med|low"}], "regression_test": "present|missing"}
```
`fix` returns findings to the lead for one round (configurable `verification.max_rounds = 1`);
`fail` stops the run with the findings in the footer. Model: `routing.verifier`, defaulting to the
escalation model; the owner's default is DeepSeek V4 Pro. Config `verification.mode = required |
advisory | off`; ambient runs (§7) may not set `off`.

### 3.3 Planner call
When the task is non-trivial (heuristic: prompt > 200 chars, or mentions ≥ 2 files, or the lead's
first plan has ≥ 3 steps), one call to `routing.planning` produces the step plan (§4.1). The lead
then executes the plan. Two or three stronger-model calls per task, no more.

### 3.4 Issue drafter
A cheap-model role that turns a trace into a diagnosis card (`problem, root_cause, impacted_files,
evidence_refs, proposed_change, test_results`) used by `/pr`, Telegram reports and the inbox.

**Exit criteria:** no `shell`/`run_tests` Tier 3 over threshold reaches the lead; verifier runs on
100 % of mutating runs on the suite; planner calls ≤ 3 per task.

---

## 4. Step-scoped execution and best-of-N (pillars 3, 10)

### 4.1 Step plan
```python
class Step(BaseModel):
    id: str; title: str; files: list[str]; done_when: str  # a verifiable condition
    validation: str | None  # command or test selector that proves this step
    attempts: int = 0; status: str = "pending"
```
Produced by the planner (§3.3) or by the lead with the existing `task` tool. Each step runs as a
bounded attempt: its own validation, its own iteration cap (`limits.step_iterations`, default 12),
its own checkpoint. A failed step is retried (§4.2) without unwinding completed steps.

### 4.2 Best-of-N with test selection
When a step's validation fails after the first attempt, `attempts.best_of` (default 3 for models
priced under $1/M input, 1 otherwise) candidate patches are generated *in parallel worktrees*
(§5) from the same step context with temperature jitter; each runs the step's validation; the
first passing candidate wins; ties broken by smallest diff. Cost is attributed per candidate.
Rationale: three Flash attempts cost less than one Sonnet call and the test, not the model,
chooses.

**Events:** `step.started/completed/failed {step_id, attempt}`, `attempt.candidate {n, passed,
diff_lines, cost}`.

---

## 5. Verify-then-surface in a worktree (pillar 10)

### 5.1 Mode
`[verification] mode = "worktree"` (default for ambient runs; interactive default `"inplace"`
until M1 is met, then `"worktree"`). In worktree mode the author edits in
`.trendlab/wt/<run_id>` (created by the existing `WorktreeManager`), validation runs there, the
verifier (§3.2) reviews there, and only then the diff is applied to the working tree (or offered
as a patch when the tree is dirty). The main tree is never touched by a failed attempt.

### 5.2 Regression-test gate
If changed files include non-test source and the task reads as a fix (heuristic + planner flag),
the evaluator requires a test that fails before the change and passes after it. The author runs
it both ways in the worktree (`git stash` of the source change for the "before" run). An explicit,
logged reason (`regression_test: "not applicable: docs only"`) is the only way around it.

### 5.3 Environment reproduction (the transcript's hard part)
Worktrees share the project's environment variables and virtualenv by default. For tests that need
services, the project may declare `[verification.services]` stubs started before validation
(§8.5). Network inside validation is off by default in worktree mode (sandbox policy), so tests
that hit the real world fail fast and visibly rather than silently.

**Exit criteria:** on the suite, zero runs leave the working tree in a failing state; 100 % of
surfaced diffs carry a passing validation log.

---

## 6. Model cocktail, attribution, per-model prompt layer (pillars 5, 8)

### 6.1 Default routing (shipped in `config.example.toml` and `trendlab init`)
```toml
[routing]
planning   = "deepseek:deepseek-v4-pro"   # 1–3 short calls per task
verifier   = "deepseek:deepseek-v4-pro"
escalation = "deepseek:deepseek-v4-pro"
screener   = "<cheapest keyed model>"      # local when it passes the screener bench
summarizer = "<cheapest keyed model>"
```
`trendlab doctor` warns when every role resolves to one model.

### 6.2 Attribution
Each model call records `role`, `phase` (plan | explore | edit | validate | verify | summarise,
inferred from tools used since the last call), `step_id`, `attempt`. `/cost --by role|phase|step`.
The benchmark report shows cost by phase so "33 % of cost" discoveries are possible.

### 6.3 Per-model prompt and skill layer (from the transcript: "tuned for Claude Code traces")
`trendlab/prompts/drivers/<provider-type>.md` and `.trendlab/skills/_model/<model>.md`: short,
editable guidance merged into the system prompt for that model only (tool-call format reminders,
"validate before finishing", known pitfalls). Seeded by hand from this week's DeepSeek sessions
(the guards now hard-coded in `rescue.py`, the re-plan guard and the evaluator nudges become
*text* here where possible), then maintained by the meta-loop (§7.4). Every heuristic guard emits
`guard.fired {guard, model}` so guards that never fire for a model can be retired for it.

### 6.4 Skill triggers
`skill.toml`: `paths`, `tools`, `keywords`, `on_failure` triggers; matching skills load into the
step's context and unload afterwards; `skill.loaded {name, trigger}`.

**Exit criteria:** cost by phase in `/cost` and bench; a Flash driver profile exists and measurably
reduces `guard.fired` counts on the suite.

---

## 7. Daemon, inbox, sleeptime memory, meta-loop (pillars 1, 6, 7, 10)

### 7.1 `trendlab-engine`
A local daemon (launchable as `trendlab engine start`, or started on demand by the TUI) owning:
the scheduler, the inbox store, the sleeptime pass, the meta-loop, and a local socket the
TUI/REPL/Telegram clients subscribe to. Sessions continue to run in the client process in phase
1; moving the agent loop into the daemon is phase 2 (§9).

### 7.2 Inbox
```sql
issue(id, project, cluster_key, title, root_cause, impacted_files, evidence_refs, first_seen,
      last_seen, occurrences, severity, status, proposed_patch_ref, verification_ref, feedback)
```
Sources: `trendlab watch` (on commit / schedule / CI failure hook), failed interactive runs,
verifier `fail` verdicts, diagnostics clusters, skill-repair proposals, meta-loop findings.
Clustering by normalised failing-test id or stack-frame signature. UI: `/inbox` screen with
`[A]pply` (runs §5 in a worktree and surfaces a verified diff), `[T]est`, `[D]ismiss`,
`[S]ilence` (writes a rule to project memory with optional reason), `[O]pen` (issue drafter
card). Telegram: a digest per cycle (counts + top three cards) instead of per-event messages;
approvals and questions stay immediate.

### 7.3 Sleeptime pass (`trendlab sleep`, scheduled nightly by the daemon)
Reads the day's sessions (messages, denials, undos, steering, inbox feedback, memory additions),
consolidates `.trendlab/memory.md` to ≤ 40 facts, proposes amendments to `TRENDLAB.md`, skills
and the per-model layer (§6.3), and opens branch `trendlab/memory-YYYY-MM-DD` with the diff for
review. `.trendlab/memory.md` and `.trendlab/skills/` become tracked files (`ensure_excluded`
narrows to the database, logs and worktrees).

### 7.4 Meta-loop (`trendlab meta`)
The harness runs its screener over its own sessions: cluster `stop_reason`, `tool.skipped`
reasons, `guard.fired`, loop detections, verifier fails and cost outliers; write inbox cards
*against the harness* with session ids as evidence; for the top card, draft a fix in a worktree of
`trendlab-cli` itself with a test. This is the transcript's "engine on engine" loop; it also
feeds §6.3 with Flash-specific findings.

**Exit criteria:** `watch` + `sleep` + `meta` run unattended for a week on the owner's projects;
inbox digest replaces per-run Telegram messages; at least one meta-loop finding fixed.

---

## 8. Evaluation: the 50-task suite, replay, stubs (pillar 9)

### 8.1 Suite
`trendlab/benchmarks/suite/` with 50 tasks across Python, TypeScript and Go, generated by
`scripts/seed_bugs.py`: clean repos with passing tests, one or more seeded defects from a
catalogue (off-by-one, wrong import, missing None check, swapped args, async misuse, race,
config typo, multi-file contract break), each with `expected_changed_files`, a hidden regression
test, and a localisation answer (file + line). Weighted toward localisation, the phase cheap
models fail most. Fixtures A–E remain as the smoke set.

### 8.2 Metrics per task
`located` (right file), `root_cause` (right line ±3), `passes` (hidden test green),
`no_collateral` (changed ⊆ expected), `regression_added`, `interventions`, `cost`, `tokens_lead`,
`tokens_total`, `wall_s`, `cost_by_phase`.

### 8.3 Containerised runs
`trendlab bench --sandbox docker` runs each task in a pinned image with the sandbox driver; local
runs remain for quick iteration.

### 8.4 Comparison and hill-climbing
`trendlab bench --compare <routing-or-profile-a> <b>`: same tasks, two configurations, deltas per
metric. M1 is this command with `flash+harness` vs `sonnet+bare` (bare = parsers, tiering,
verifier, best-of-N and planner off). Weekly cadence (from the transcript): hypothesis → eval →
implement; results appended to `docs/BENCH_LOG.md`.

### 8.5 Stubs
`trendlab stub --spec <openapi.yaml|recorded>`: a local HTTP stub server the agent may start for
projects whose tests call external services; `[verification.services]` lists stubs to launch
before validation. Recorded mode captures real responses once (allow-listed hosts) for replay.

### 8.6 Shadow replay
`trendlab replay <session-id> [--build <ref>]`: re-run a stored session's prompts against the
current build, feeding recorded tool results where the call signature matches, and diff tool
choices, transcript length, cost and outcome. Used before each release on the owner's real
sessions, as the transcript's shadow production.

**Exit criteria:** 50 tasks run green under `docker`; M1 report produced; replay runs on ≥ 20 of
the owner's sessions without crashes.

---

## 9. Delivery plan

| Phase | Scope | Exit criterion | Est. |
|---|---|---|---|
| P1 Tiering | §2 (ToolOutput, 8 parsers, inspect_output, budgets, baselines) + screener §3.1 | lead tokens −40 % on A–E, outcomes unchanged | 2 nights |
| P2 Verify | §5 worktree mode, verifier §3.2, regression gate §5.2 | 0 failing trees on A–E; 100 % verified diffs | 2 nights |
| P3 Steps | §4 step plan, planner call §3.3, best-of-N §4.2 | fixture pass rate on Flash ≥ Sonnet-bare on A–E | 2 nights |
| P4 Cocktail | §6 routing defaults, phase attribution, Flash driver profile, skill triggers | `/cost --by phase`; `guard.fired` down on Flash | 1 night |
| P5 Suite | §8.1–8.4 fifty tasks, docker runs, `--compare`, M1 measured | M1 report | 3 nights |
| P6 Daemon | §7 engine, inbox, digest, sleep, meta; §8.5–8.6 stubs and replay | one unattended week | 4 nights |

Each phase: its own commits with tests, spec section updated the same night, Telegram report,
no change to locked rules (auto default, hard boundaries, irreversible prompt, audit, checkpoint,
jet-black/neon UI).

---

## 10. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Tiering hides the one line the model needed (the transcript's "we pre-filtered what the agent could have decided") | Tier 1 always includes counts and the first failing id; `inspect_output` is cheap and advertised in the policy text; the screener receives the lead's question, not a generic "summarise". |
| Verifier and planner on a stronger model erode the cost advantage | Hard cap: ≤ 3 planner/verifier calls per task; M1's ≤ 10 % cost bound is measured, not assumed. |
| Best-of-N multiplies cost on hard tasks | Only after a first failure, only for cheap models, capped at 3, attributed and visible. |
| Worktree mode slows interactive use | Interactive default stays in-place until M1 is met; worktree creation is cached per run. |
| Local screener models misjudge | Admission by the screener benchmark only; otherwise the cheapest API model. |
| More harness code contradicts "strip back determinism" | Guards become editable text per model; `guard.fired` counts justify every guard's existence; parsers are pure and tested, not workflows. |
| Memory and skills drift | Sleeptime consolidation with a hard cap; changes land as reviewable branches. |

---

## 11. What is explicitly not promised

Flash will not out-design Sonnet on architecture-heavy tasks; the planner call exists for that
reason. Real rate-limit and real context-overflow responses still cannot be forced in drills.
Container runs require Docker on the machine. The daemon moves the agent loop out of the client
only in a later phase; phase 1 keeps the loop in-process and the daemon beside it.
