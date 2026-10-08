"""Semantic retrieval (uplift U16): embeddings, a vector index and hybrid ranking.

* Embedders: Ollama (``/api/embed``, local and free) or any OpenAI-compatible ``/embeddings``.
  Models that expect task prefixes (nomic-embed-text) get them.
* ``VectorIndex``: vectors cached on disk by content hash in ``.trendlab/index/``, so only
  changed chunks are embedded again. Search is exact cosine over normalised vectors — at the
  size of one repository (thousands of chunks) exact search takes milliseconds, so an
  approximate-nearest-neighbour structure would add error and gain nothing.
* ``hybrid``: reciprocal-rank fusion of the BM25 and vector rankings; lexical matching finds
  identifiers, embeddings find meaning ("drops the last element" → an off-by-one in a range).
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import httpx

from trendlab.context.retrieval import Chunk
from trendlab.context.retrieval import search as bm25_search

BATCH = 32
_PREFIX = {"nomic": ("search_document: ", "search_query: ")}


def _prefixes(model: str) -> tuple[str, str]:
    for key, pair in _PREFIX.items():
        if key in model:
            return pair
    return ("", "")


class Embedder:
    def __init__(self, kind: str, model: str, base_url: str, api_key: str | None = None) -> None:
        self.kind, self.model, self.base_url, self.api_key = (
            kind,
            model,
            base_url.rstrip("/"),
            api_key,
        )
        self.doc_prefix, self.query_prefix = _prefixes(model)
        self.calls = 0

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        prefix = self.query_prefix if query else self.doc_prefix
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH):
            batch = [prefix + t[:8000] for t in texts[i : i + BATCH]]
            self.calls += 1
            if self.kind == "ollama":
                base = self.base_url.removesuffix("/v1")
                r = httpx.post(
                    f"{base}/api/embed", json={"model": self.model, "input": batch}, timeout=120
                )
                r.raise_for_status()
                out.extend(r.json()["embeddings"])
            else:
                headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
                r = httpx.post(
                    f"{self.base_url}/embeddings",
                    headers=headers,
                    json={"model": self.model, "input": batch},
                    timeout=120,
                )
                r.raise_for_status()
                out.extend(
                    d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])
                )
        return [_normalise(v) for v in out]


def make_embedder(config, ref: str) -> Embedder:
    """``provider:model`` from [providers]; ollama uses its native endpoint."""
    from trendlab.security.secrets import resolve_secret

    name, _, model = ref.partition(":")
    pc = config.providers.get(name)
    if pc is None:
        raise ValueError(f"unknown provider {name!r} for embeddings")
    kind = "ollama" if pc.type == "ollama" else "openai"
    base = pc.base_url or ("http://localhost:11434" if kind == "ollama" else "")
    key = resolve_secret(pc.api_key_env) if kind != "ollama" and pc.api_key_env else None
    return Embedder(kind, model, base, key)


def _normalise(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _hash(c: Chunk) -> str:
    return hashlib.sha1(f"{c.path}\n{c.text}".encode()).hexdigest()[:16]


class VectorIndex:
    def __init__(self, path: Path, embedder: Embedder) -> None:
        self.path, self.embedder = path, embedder
        self.vectors: dict[str, list[float]] = {}
        try:
            data = json.loads(path.read_text())
            if data.get("model") == embedder.model:
                self.vectors = data.get("vectors", {})
        except (OSError, ValueError):
            pass

    def build(self, chunks: list[Chunk]) -> int:
        """Embed chunks not seen before; returns how many were embedded."""
        missing = [c for c in chunks if _hash(c) not in self.vectors]
        if missing:
            vecs = self.embedder.embed([f"{c.path}\n{c.text}" for c in missing])
            for c, v in zip(missing, vecs, strict=True):
                self.vectors[_hash(c)] = v
            live = {_hash(c) for c in chunks}
            self.vectors = {h: v for h, v in self.vectors.items() if h in live}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"model": self.embedder.model, "vectors": self.vectors})
            )
        return len(missing)

    def search(self, chunks: list[Chunk], query: str, k: int = 3) -> list[tuple[Chunk, float]]:
        if not chunks:
            return []
        q = self.embedder.embed([query], query=True)[0]
        scored = []
        for c in chunks:
            v = self.vectors.get(_hash(c))
            if v is not None:
                scored.append((c, sum(a * b for a, b in zip(q, v, strict=True))))
        scored.sort(key=lambda cs: -cs[1])
        return [(c, round(s, 4)) for c, s in scored[:k]]


def hybrid(
    lexical: list[tuple[Chunk, float]],
    semantic: list[tuple[Chunk, float]],
    k: int = 3,
    c: int = 60,
) -> list[tuple[Chunk, float]]:
    """Reciprocal-rank fusion: score = Σ 1 / (c + rank) over the rankings a chunk appears in."""
    score: dict[tuple[str, int], float] = {}
    by_key: dict[tuple[str, int], Chunk] = {}
    for ranking in (lexical, semantic):
        for rank, (ch, _s) in enumerate(ranking, 1):
            key = (ch.path, ch.start)
            by_key[key] = ch
            score[key] = score.get(key, 0.0) + 1.0 / (c + rank)
    ranked = sorted(score.items(), key=lambda kv: -kv[1])[:k]
    return [(by_key[key], round(s, 5)) for key, s in ranked]


def retrieve(
    chunks: list[Chunk],
    query: str,
    *,
    mode: str = "bm25",
    index: VectorIndex | None = None,
    k: int = 3,
) -> list[tuple[Chunk, float]]:
    if mode == "bm25" or index is None:
        return bm25_search(chunks, query, k=k)
    index.build(chunks)
    if mode == "vector":
        return index.search(chunks, query, k=k)
    pool = max(10, k * 4)
    return hybrid(bm25_search(chunks, query, k=pool), index.search(chunks, query, k=pool), k=k)


def index_path(root: Path) -> Path:
    return root / ".trendlab" / "index" / "vectors.json"


def session_text(store, session_id: str) -> str:
    """What a session was about: its user prompts and final answers (for semantic search)."""
    parts: list[str] = []
    for m in store.messages(session_id):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            parts.append(m["content"][:600])
    for e in store.events(session_id, "run.completed"):
        parts.append(str((e.get("data") or {}).get("text") or "")[:600])
    return "\n".join(p for p in parts if p)[:4000]


def semantic_sessions(
    store, embedder: Embedder, query: str, *, cache: Path, limit: int = 300, k: int = 10
) -> list[dict[str, Any]]:
    """Rank recent sessions by meaning, with vectors cached per session content."""
    try:
        cached = json.loads(cache.read_text()) if cache.is_file() else {}
    except ValueError:
        cached = {}
    if cached.get("model") != embedder.model:
        cached = {"model": embedder.model, "vectors": {}}
    rows = []
    texts, keys = [], []
    for s in store.sessions(limit=limit):
        text = session_text(store, s["id"])
        if not text:
            continue
        key = f"{s['id']}:{hashlib.sha1(text.encode()).hexdigest()[:10]}"
        rows.append((s, key, text))
        if key not in cached["vectors"]:
            texts.append(text)
            keys.append(key)
    if texts:
        for key, v in zip(keys, embedder.embed(texts), strict=True):
            cached["vectors"][key] = v
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(cached))
    q = embedder.embed([query], query=True)[0]
    scored = []
    for s, key, text in rows:
        v = cached["vectors"][key]
        scored.append(
            {
                "session": s["id"],
                "project": s.get("project_path"),
                "updated_at": s.get("updated_at"),
                "score": round(sum(a * b for a, b in zip(q, v, strict=True)), 4),
                "preview": text[:160].replace("\n", " "),
            }
        )
    scored.sort(key=lambda r: -r["score"])
    return scored[:k]
