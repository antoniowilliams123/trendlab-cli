# Live measurements (2026-10-08): judge bias, drift, sandbox isolation, sensitivity sweep, load
# curve.
R = [
    (165, 9, "judge-bias probe live: v4-pro verifier 0/8 verdict flips under padding or restyling"),
    (166, 9, "same"),
    (168, 9, "verbosity bias 0/8 live"),
    (169, 9, "style bias 0/8 live"),
    (140, 9, "drift on real data: suite vs 159 real sessions, kind 0.23, length 0.42 (short prompts under-represented)"),
    (143, 9, "same, plus last-7-days vs previous period"),
    (34, 9, "bubblewrap sandbox live: project writable; home read-only; /tmp private; network blocked"),
    (33, 9, "same isolation + run lock + worktrees"),
    (198, 9, "sweep live: verify-everything vs gated verifier — 6/6 both, cost $0.056 vs $0.031"),
    (147, 9, "load curve live: 4 → 11.8 tasks/min, 8 → 16.1, 16 → 9.2 (p95 96 s); 0 crashes"),
    (43, 9, "iteration, cost, call, wall-clock, token and latency budgets, each with a failure code"),
    (454, 9, "OS sandbox isolation measured + taints + per-category policy"),
]
