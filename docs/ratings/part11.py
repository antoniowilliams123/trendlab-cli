# Model comparison (canary, harness profile, 2026-10-08): Flash 10/10 $0.091 355 s; v4-pro
# 10/10 $0.191 387 s; local 25B on Ollama 1/2 finished, $0, ~21 min/task. Plus token
# calibration, compaction cap and ledger heavy-call share.
R = [
    (795, 9, "Flash vs v4-pro vs local Ollama on the canary: pass, cost, wall time measured"),
    (796, 9, "cloud $0.091/$0.191 for 10 tasks vs local $0 at ~21 min/task"),
    (784, 9, "Flash substitutes v4-pro at equal pass and half the cost; also planner, critic, reviewer"),
    (783, 9, "v4-pro premium measured: 2.1x cost with no canary gain; 3.5x as planner with no gain"),
    (800, 9, "the harness runs a local 25B model and local embeddings through Ollama"),
    (725, 8, "local model failed missing_none_check where both cloud models passed"),
    (735, 7, "canary saturates for cloud models; the gap shows only on the local model and hard tiers"),
    (737, 8, "bare vs harness measured per tier for Flash; harness vs model across three models"),
    (723, 9, "capability map per tier and defect + per-model comparison"),
    (724, 9, "same: blind spots per model visible (local: none-checks)"),
    (740, 9, "same"),
    (155, 6, "hard tier (cross-module, misleading symptoms) per model; not a reasoning benchmark"),
    (732, 6, "per-model scorecard on the canary"),
    (742, 6, "design critique eval found 15/15 planted flaws for Flash and v4-pro alike"),
    (743, 6, "same"),
    (744, 6, "performance lens in review; N+1 and stampede flaws found in the critique eval"),
    (426, 8, "chars-per-token learned per model from usage; len/4 undercounted 6-26% (84a05f3)"),
    (427, 7, "provider usage is the tokenizer of record; no local tokenizer"),
    (788, 8, "ledger: p95 input tokens and spend share above 30k; home sessions 34% (52a8317)"),
    (761, 9, "heavy-prompt share + cache share + compaction cap (3693acc)"),
    (749, 9, "cache share and large-prompt share per project"),
    (759, 9, "same, per role and model"),
    (760, 9, "calibrated budgets, compaction cap, tiered output, quick review"),
]
