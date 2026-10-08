# Re-ratings after U20 deterministic replay, U21 PR workflow, U22 compaction eval (2026-10-08).
R = [
    # U20 deterministic replay (ca50dc8, 5e76e9d)
    (32, 9, "cassettes of every model and command response; 8/8 real sessions replay identically, 70 calls, $0 (5e76e9d)"),
    (184, 9, "shadow replay with a new build + deterministic replay of real sessions as a regression suite (5e76e9d)"),
    (186, 9, "suite, planning, review, retrieval, compaction, delegation and replay evals run offline/recorded"),
    (135, 9, "hidden-test regression gate per run + replay regression corpus of real sessions (5e76e9d)"),
    (31, 9, "file checkpoints + cassettes: any session can be rebuilt as it was before it ran (ca50dc8)"),
    (30, 9, "sessions, events, checkpoints and cassettes persisted; resume and replay"),
    (710, 9, "trace data incl. raw model and command results kept per session, pruned with retention"),
    (712, 9, "spans + per-call cassette records"),
    (713, 9, "execution paper trail down to every model response"),
    # U21 PR workflow (0bc40cc)
    (806, 8, "`trendlab pr list/stats/workspace` + inbox + dashboard form a maintainer console"),
    (807, 8, "PR triage, async commit review and review gates automated"),
    (808, 9, "`trendlab pr stats`: merged/week, hours to merge (median, p90); live on cli/cli (0bc40cc)"),
    (809, 9, "`trendlab pr list`: next action per PR, quickest unblocking work first; live on 30 PRs (0bc40cc)"),
    (810, 8, "triage + review --pr + PR workspaces; no auto-merge by design"),
    (811, 8, "PR worktrees under .trendlab/pr-worktrees keep the agent beside the checkout"),
    (812, 9, "`trendlab pr workspace N --agent` runs the agent in the PR's own worktree (0bc40cc)"),
    (603, 9, "merges are never automatic; ready-to-merge is a suggestion"),
    # U22 compaction (0dc8673)
    (88, 9, "compaction eval: model summary keeps 30/30 planted facts at 14% of tokens (0dc8673)"),
    (87, 9, "fallback compaction fixed from 0% to 100% retention at 9% of tokens (0dc8673)"),
    (86, 8, "layered context (system, map, summary, plan, recent) with budgets; retention measured"),
    (84, 9, "context layers, tiered tool output, retrieval hints, compaction — each measured"),
    (28, 9, "structured state + compaction keep working facts; 100% retention measured (0dc8673)"),
    (85, 9, "context window tracked per model; compaction before overflow; overflow recovery"),
    (90, 8, "project memory injected into the system prompt; learning is gated and bounded"),
]
