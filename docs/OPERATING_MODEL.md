# Working with TrendLab: the operating model

How a change moves from request to merged code, who does what at each stage, and the number
that says whether the stage works. Every command below exists; every number was measured on
this machine (sources: `docs/TRENDLAB_CLI_SPEC.md` §93.8, `docs/ratings/`).

| Stage | Who | What happens | Command / config | Measured |
|---|---|---|---|---|
| 1. Intake | agent | The request is classified (question, small fix, feature, risky) and gets matching settings | `[routing] router` | router 100 % on 40 held-out prompts (few-shot) |
| 2. Plan | agent, person | Non-trivial work becomes a dependency graph of verifiable steps; the plan is linted; the person can approve, or reject *with a reason* and get a revised plan | `[planner]`, `[plan_gate]` | Flash planner 97 % file recall on 16 hard tasks; co-design test |
| 3. Implement | agent | Edits inside the change scope; each edit checked for parse errors, unresolved names and new import cycles | `--allow-path`, `[governance]` | 0 false alarms on 1,078 real files (symbol check) |
| 4. Verify | harness | Tests run after edits; a fresh-context verifier judges the diff; findings go back once | `[verification]` | 98.9 % of runs that saw a failing check ended passing |
| 5. Review | harness, person | Branch/PR review with a findings ledger; re-check closes findings; pre-PR gate | `trendlab review [--pre-pr]` | 32/32 seeded defects caught, 3 % high false alarms |
| 6. Trace | harness | Every turn committed on a side branch; every model response recorded | `commit_per_turn`, cassettes | 8/8 real sessions replay identically at $0 |
| 7. Ship | person | Merge stays with the person; the harness never merges or pushes | `trendlab pr list` | — |
| 8. Observe | person | Live events, trace trees, root causes, costs | `trendlab tail / trace / rca / cost` | fully loaded cost per project |
| 9. Learn | harness | Durable facts from runs go to project memory; context compaction keeps what matters | `[memory]` | memory 8/8 durable kept, 0/4 noise; compaction 100 % at 9–14 % of tokens |
| 10. Monitor | engine | Nightly canary, daily health, commit review, budgets, retention | `trendlab engine start` | chaos 10/10 under 20 % faults |
| 11. Improve | person + harness | Failures coded and ranked, regressions replayed, every rating change tied to evidence | `trendlab failures`, `replay --recent`, ledger | 0 unclassified failures in 411 runs |

## Roles

- **The person** sets intent and limits (change scope, budgets, communication rules), approves
  plans and risky actions, merges, and reads the cards the engine files.
- **The agent** plans, edits and reports inside those limits; the constitution
  (`docs/AGENT_CONSTITUTION.md`) lists the rules it is held to and how each is checked.
- **The harness** enforces, measures and records: nothing it claims about the agent is
  unmeasured, and every measurement can be re-run with `trendlab bench`.
