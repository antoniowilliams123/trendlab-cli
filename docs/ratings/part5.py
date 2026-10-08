# Re-ratings after U16 semantic retrieval, U17 hallucinated references, U18 sampling, U19 spec
# traceability and the review-mode measurement (2026-10-08).
R = [
    # U16 semantic retrieval (a4c6b2f, 44f92d9)
    (120, 9, "nomic-embed-text embeddings (local Ollama or OpenAI-compatible); vector recall@1 83% vs BM25 67% on 64 suite tasks (a4c6b2f, 44f92d9)"),
    (119, 9, "cosine similarity over normalised embeddings ranks code and sessions by meaning (a4c6b2f)"),
    (346, 7, "on-disk vector store keyed by content hash; not a database server, by design at repo scale"),
    (382, 8, "embedding store cached per content hash, rebuilt on model change (a4c6b2f)"),
    (383, 8, "vector index with incremental updates and stale pruning (a4c6b2f)"),
    (384, 5, "exact search on purpose: at repository scale it is milliseconds; no ANN structure"),
    (385, 9, "semantic code retrieval and `trendlab search --semantic` over sessions (a4c6b2f)"),
    (439, 8, "retrieved chunks injected as labelled hints (BM25/vector/hybrid); live A/B showed no pass gain on the saturated suite"),
    (89, 8, "retrieval hints with three modes and BM25 fallback; off by default after a no-gain A/B"),
    (153, 9, "bench --retrieval-eval: recall@1/3/5 and MRR per mode, symptom-only split (a4c6b2f)"),
    (699, 9, "`trendlab search --semantic` ranks past sessions by meaning (a4c6b2f)"),
    (118, 9, "BM25 with stemming + reference similarity; retrieval recall@3 97% (44f92d9)"),
    # U17 hallucinated references (18b6be9)
    (581, 9, "hallucinated imports/APIs caught on the lines an edit adds; 0 false alarms on 1,078 real files, 5/5 seeded (18b6be9)"),
    (566, 6, "unresolved names caught; invented edge cases in tests are not checked"),
    (559, 6, "edits that reference things that do not exist are flagged; reasoning errors are not"),
    (124, 9, "unsupported-claims rate + hallucinated-reference rate per bench row (380a06e, 18b6be9)"),
    (605, 9, "claims checked against evidence; references resolved against the code (18b6be9)"),
    # U18 sampling (6d06cc3)
    (433, 8, "per-role sampling policy reaches the provider request (6d06cc3)"),
    (434, 8, "judging roles at temperature 0; measured: Flash verifier agreed with itself on 11/12 diffs at both settings (6d06cc3)"),
    (435, 6, "top_p configurable per role; not measured"),
    (590, 9, "verifier self-agreement measured (92% of diffs identical across 5 repeats); flaky tasks via --runs (6d06cc3)"),
    # U19 spec traceability (44f92d9)
    (529, 9, "`trendlab spec`: each requirement implemented/partial/missing with verified evidence (44f92d9)"),
    (530, 8, "spec-first workflow supported; spec authoring itself is manual"),
    (532, 9, "requirements extracted from lists or prose (44f92d9)"),
    (551, 9, "spec drift: requirements whose status got worse since the last check (44f92d9)"),
    (647, 9, "spec eval with known answers: Flash 100% on 10 requirements, evidence 100% verified, $0.006 (44f92d9)"),
    (646, 8, "verifier judges against the request; spec check against the written intent"),
    (550, 8, "spec drift + verifier against the original task"),
    (848, 6, "plan lint and spec check before/after; no architecture review step"),
    (850, 7, "scope budget from the task before edits"),
    # review mode measurement (f5e08f7)
    (544, 8, "structure lens in every review"),
    (546, 9, "edge cases lens; quick+confirm review caught 32/32 seeded defects (f5e08f7)"),
    (583, 9, "verifier + confirmation-pass review: blind acceptance is the thing being measured (f5e08f7)"),
    (829, 9, "every surfaced change is verified; reviews are confirmed by a second pass (f5e08f7)"),
    (830, 9, "same; noise on correct fixes cut from 1.16 to 0.16 findings (f5e08f7)"),
    (845, 9, "verifier precision/recall, review recall and false alarms all measured"),
    (846, 9, "same"),
    # critique panel (both planted-flaw sets saturated)
    (519, 8, "design panel works; a single Flash critic already found 15/15 planted flaws, so panel gain is unmeasured"),
]
