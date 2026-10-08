# The user-felt six (2026-10-08): invented edge cases, paper cuts, plain language, design before
# code, multi-agent. Each backed by a live A/B or a full-suite run.
R = [
    (559, 9, "reference-fit check runs the agent's tests on the correct code; invented inputs "
     "4/108 runs before the scope rule, 0/50 after; 0 out-of-scope source edits in 122 runs"),
    (566, 9, "same check in every bench row (invented_inputs vs own-wording split); "
     "0 invented edge cases in 50 runs since the rule"),
    (571, 9, "post-edit lint fed back live (93 real fires); 0 new lint findings in 113 final "
     "diffs; reviewer caught and located 32/32 small seeded bugs"),
    (586, 9, "buzzword dictionary in answer metrics; 0 of 586 real answers and 0 of 44 live "
     "runs used one; plain mode flags and rewrites them"),
    (629, 9, "tone scored: hype, apologies, hedges, exclamations; enforced when a tone is set; "
     "live A/B: 0 tone issues, same 22/22 passes"),
    (631, 8, "plain mode live A/B: 11.7 → 7.8 words per sentence, readability 68 → 80, long "
     "sentences 5 → 0, acronyms 4 → 0, passive-voice rule; no STE controlled dictionary"),
    (848, 9, "plans state approach/interfaces/reuse before steps (16/16 plans); declared "
     "interfaces implemented; design checkpoint holds multi-file edits in unplanned runs "
     "(fired 3/3, all passed)"),
    (22, 9, "delegation on all 81 suite tasks: located 81/81, bug line 100%, handoffs complete "
     "and evidence verified 100%; orchestrator-worker + critic panel"),
]
