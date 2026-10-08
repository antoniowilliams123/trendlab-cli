"""Uplift U16: embeddings, vector index, hybrid retrieval, retrieval eval, semantic sessions."""

import hashlib
import math
from datetime import UTC, datetime
from pathlib import Path

from trendlab.config.schema import AppConfig
from trendlab.context.retrieval import Chunk
from trendlab.context.vectors import (
    VectorIndex,
    _normalise,
    hybrid,
    make_embedder,
    retrieve,
    semantic_sessions,
)


class FakeEmbedder:
    """Bag-of-words hashed into 64 dims: similar words → similar vectors, deterministic."""

    model = "fake"

    def __init__(self):
        self.calls = 0
        self.texts = 0

    def embed(self, texts, *, query=False):
        self.calls += 1
        self.texts += len(texts)
        out = []
        for t in texts:
            v = [0.0] * 64
            for w in t.lower().replace("_", " ").split():
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1.0
            out.append(_normalise(v))
        return out


CHUNKS = [
    Chunk("shop/util.py", 1, 3, "def chunks(items, size):\n    return split list into size groups"),
    Chunk("shop/tax.py", 1, 3, "def tax(amount):\n    return amount times rate"),
    Chunk("shop/stock.py", 1, 3, "def low(levels):\n    return skus below threshold"),
]


def test_vector_index_caches_by_content(tmp_path: Path):
    emb = FakeEmbedder()
    idx = VectorIndex(tmp_path / "v.json", emb)
    assert idx.build(CHUNKS) == 3 and emb.texts == 3
    again = VectorIndex(tmp_path / "v.json", emb)  # reloaded from disk
    assert again.build(CHUNKS) == 0 and emb.texts == 3  # nothing re-embedded
    changed = [*CHUNKS[:2], Chunk("shop/stock.py", 1, 3, "def low(levels): new body")]
    assert again.build(changed) == 1 and len(again.vectors) == 3  # stale vector pruned
    hits = again.search(CHUNKS, "split items into size groups", k=1)
    assert hits[0][0].path == "shop/util.py"
    other_model = FakeEmbedder()
    other_model.model = "other"
    assert VectorIndex(tmp_path / "v.json", other_model).vectors == {}  # model change: rebuild


def test_hybrid_rank_fusion_rewards_agreement():
    a, b, c = CHUNKS
    lexical = [(a, 5.0), (b, 4.0), (c, 1.0)]
    semantic = [(c, 0.9), (a, 0.8), (b, 0.1)]
    fused = hybrid(lexical, semantic, k=3)
    assert [ch.path for ch, _ in fused] == ["shop/util.py", "shop/stock.py", "shop/tax.py"]
    assert math.isclose(fused[0][1], round(1 / 61 + 1 / 62, 5))


def test_retrieve_modes(tmp_path: Path):
    emb = FakeEmbedder()
    idx = VectorIndex(tmp_path / "v.json", emb)
    q = "skus below threshold"
    assert retrieve(CHUNKS, q, mode="bm25", k=1)[0][0].path == "shop/stock.py"
    assert retrieve(CHUNKS, q, mode="vector", index=idx, k=1)[0][0].path == "shop/stock.py"
    assert retrieve(CHUNKS, q, mode="hybrid", index=idx, k=1)[0][0].path == "shop/stock.py"
    assert retrieve(CHUNKS, q, mode="vector", index=None, k=1)  # no index: BM25 fallback


def test_make_embedder_kinds():
    cfg = AppConfig()
    from trendlab.config.schema import ProviderConfig

    cfg.providers["ollama"] = ProviderConfig(type="ollama", base_url="http://localhost:11434/v1")
    e = make_embedder(cfg, "ollama:nomic-embed-text")
    assert e.kind == "ollama" and e.doc_prefix == "search_document: "
    cfg.providers["oai"] = ProviderConfig(type="openai_compatible", base_url="https://x/v1")
    assert make_embedder(cfg, "oai:text-embedding-3-small").kind == "openai"


def test_retrieval_eval_scores_modes():
    from trendlab.benchmarks.runner import retrieval_eval
    from trendlab.benchmarks.suite import get_task

    tasks = [get_task("py01-off_by_one"), get_task("py03-missing_none_check")]
    res = retrieval_eval(tasks, ["bm25", "vector", "hybrid"], FakeEmbedder())
    for mode in ("bm25", "vector", "hybrid"):
        s = res["modes"][mode]["all"]
        assert s["n"] == 2 and 0 <= s["recall@1"] <= s["recall@3"] <= s["recall@5"] <= 1
    assert res["rows"][0]["task"] == "py01-off_by_one"


def test_semantic_sessions_rank_and_cache(tmp_path: Path):
    from trendlab.sessions.store import SessionStore

    store = SessionStore(tmp_path / "s.db")
    now = datetime.now(UTC).isoformat()
    for prompt in ("fix the tax rounding bug", "add csv export of orders"):
        sid = store.create_session("/p", "m", "m")
        store.append_message(sid, {"role": "user", "content": prompt})
        store.append_event(sid, "run.completed", {"text": "done: " + prompt}, now)
    emb = FakeEmbedder()
    top = semantic_sessions(store, emb, "tax rounding", cache=tmp_path / "c.json")
    assert top[0]["preview"].startswith("fix the tax rounding bug")
    first = emb.texts
    semantic_sessions(store, emb, "csv", cache=tmp_path / "c.json")
    assert emb.texts == first + 1  # only the query embedded the second time
    store.close()
