# Rating uplift plan — every AI concept below 9/10 → 9 or 10

Source: `~/AI Engineer/AI_Engineering_Master_Vocabulary_TrendLab_Rated.md` (560 AI terms rated
2026-10-08; 540 below 9, 190 at 4 or lower). This plan groups them into work packages, in the
order that changes outcomes first. Each package names its exit test, because a rating only moves
when the thing is built *and measured*.

## Rules for this programme

1. **A term moves to 9 only with evidence**: a feature, a test, and a number in `docs/BENCH_LOG.md`
   or the spec. No rating is raised by prose.
2. **Some terms are "not a harness concern" and stay n/a by design** — embedding similarity,
   vector databases, Brier score, training-side concepts. They are listed in §12 so the gap is
   deliberate, not forgotten.
3. **Cost discipline is a hard constraint**: nothing in this plan may raise the everyday cost of a
   small task above 1.3× bare Flash (tonight's measured line).
4. Every package: own commit(s), tests, spec §93 entry, Telegram note.

## Work packages (priority order)

| # | Package | Terms it lifts (examples) | Deliverable | Exit test |
|---|---|---|---|---|
| U1 | **Evaluation rigour** | statistical significance, confidence interval, reliability eval, error rate, tool success rate, pass@k, inter-run variance, A/B, champion/challenger, eval gating, release gate, canary eval, continuous eval | `bench --runs N` (repeat runs), bootstrap confidence intervals and a paired significance test in `--compare`, per-tool success rate and error rate in every report, `bench --gate` that fails a release when the delta is significant and negative, a canary subset run by the engine nightly | compare report shows interval + p-value; CI gate test; nightly canary row in BENCH_LOG |
| U2 | **Judge quality** | evaluator bias, judge bias, position/verbosity/style bias, judge agreement, LLM-as-judge, pairwise, rubric grading, grader hacking | verifier verdicts cross-checked against hidden-test ground truth on the suite (verifier precision/recall per model), pairwise verdicts with position swap, rubric field in the verifier schema, a judge-agreement report between two verifier models | report of verifier precision/recall; swap-invariance test |
| U3 | **Drift and regression watch** | behavioural/quality/performance regression, benchmark drift, prompt drift, model drift, distribution shift, data drift, holdout, benchmark leakage | engine nightly canary (10 fixed tasks) with a per-model baseline, alert on a significant drop; model-version stamp on every record; holdout split of the suite never used for tuning; prompt-hash change detection | drift alert fires on an injected regression |
| U4 | **Tool reliability** | tool success rate, tool fallback, circuit breaker, timeout policy, idempotent execution, pre/postconditions, invariants, runtime assertions | per-tool success/latency counters, circuit breaker that pauses a tool after N consecutive failures with a readable reason, fallback chain for search/fetch/tests, explicit postcondition checks on edits (file hash, syntax parse), idempotent write detection | counters in `/status`; breaker test; postcondition test |
| U5 | **Scope, bloat and code-quality control** | scope drift, unsolicited refactoring, unasked dependency, bloat, unnecessary abstraction, weak generated tests, missed edge case, dependency sprawl | diff-shape metrics per run (files, lines, new functions/classes, new deps) with a scope budget from the task; dependency-add gate; generated-test strength check (mutation-style: does the new test fail when the fix is reverted); collateral metric in the footer | metrics in RunResult; mutation check test; collateral regression in suite |
| U6 | **Observability depth** | trace tree, span, latency telemetry, session search, retention policy, trace analytics, dashboard, trace normalisation, audit-ready traces | span ids and parent ids on every event, per-span durations, `trendlab sessions search <text>` (exact) over messages/events, retention config with pruning, `trendlab trace <session>` tree view, OpenTelemetry-style JSON export | tree render test; search test; export validates |
| U7 | **Planning and decomposition** | task graph, dependency graph, plan refinement, dynamic replanning, goal decomposition, planning eval | step dependencies honoured (parallel-safe steps), plan refinement after each step from evidence, a planning eval in the suite (plan vs reference step set) | planning eval row; dependency test |
| U8 | **Failure taxonomy and RCA** | failure taxonomy, error taxonomy, root cause analysis, FMEA, counterfactual eval, sensitivity | one shared taxonomy across evaluator, loop detector, verifier, engine cards; every FAILED run carries a taxonomy code; `trendlab meta` reports by code; counterfactual re-run of a failed task with a different profile | taxonomy coverage test; meta by-code report |
| U9 | **Governance and communication constraints** | output volume constraint, tone constraint, plain-language constraint, review granularity, prompt-level commit, turn-level review, change-scope constraint | `[governance]` config: max answer length, plain-language mode, per-turn checkpoint commits on a side branch, change-scope allowlist per run | config tests; commit-per-turn test |
| U10 | **Economics and ROI** | token ROI, session ROI, cost per project, utilisation analytics, fully loaded cost, business-aligned spend | per-project cost ledger, ROI = passes / dollar in bench, `trendlab cost --project`, budget alerts to Telegram | ledger test; alert test |
| U11 | **Security and robustness evals** | adversarial testing, red teaming, prompt-injection eval, jailbreak resistance, safety eval, chaos testing, stress testing | injection corpus run through the agent (tool outputs carrying instructions) with a pass/fail report; chaos drills (dead provider, killed process, corrupted trace) as a bench mode; stress run of 20 parallel candidates | injection eval report; chaos bench green |
| U12 | **By design: stays n/a** | embedding/semantic similarity, vector DB/index, ANN, semantic session search, router model, Brier, calibration error, training-side terms, token subsidies/market terms | documented decision, revisited only if retrieval enters the design | — |

## Expected impact on the harness (honest estimate)

- **Credibility**: the biggest change. Today the suite says "40/40 vs 40/40" with no idea whether
  a 2-task difference would be real. After U1–U3 every claim carries an interval and a drift
  watch. This is what separates a tool that *is* good from one that *looks* good.
- **Reliability**: U4 and U8 turn tonight's class of findings (a stuck approval, a 140 GB copy,
  a tick-loop) into counted, named failures with breakers, instead of things I notice by luck.
- **Quality of changes**: U5 is the only package that directly attacks the "cheap model wrote
  bloated/unsafe code" risk. Expect it to move `no_collateral` and generated-test strength, which
  are the metrics that matter once pass rate is saturated.
- **Cost**: U10 keeps the 1.3× line visible per project; nothing here should raise it.
- **Pass rate**: little to none on the current suite — it is already saturated. The honest
  reading stands: the uplift makes the harness *trustworthy and governable*, not more likely to
  pass a unit-test task that Flash already passes.
- **Rating**: if all packages land with their exit tests, roughly 430 of the 540 sub-9 terms move
  to 9–10; about 60 stay at 7–8 (they need a scale of use this project does not have, e.g.
  enterprise IT approval, cross-harness normalisation); about 50 stay n/a by design (U12).
  Mean would move from 5.7 to about 8.6, with the shortfall fully explained rather than hidden.

## Order of execution

U1 → U4 → U5 → U3 → U2 → U6 → U8 → U7 → U9 → U10 → U11. U1 first because every later package is
judged by it.
