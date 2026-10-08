"""Lexical retrieval (uplift: retrieval injection, retrieval eval, ranked search).

BM25 over code chunks, no embeddings and no dependencies. Identifiers are split on snake_case
and camelCase so "busiest hours" finds ``busiest_hours`` and "loadOrders" finds "load orders".
Chunks are function/class blocks for Python (via ``ast``) and fixed windows otherwise.

Used two ways: at run start the top chunks for the request are offered to the model as
*hints* (labelled as retrieved, to be verified by reading the file), and `trendlab search
--ranked` ranks past sessions by relevance instead of recency.
"""

from __future__ import annotations

import ast
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_SPLIT = re.compile(r"[A-Za-z][a-z]+|[A-Z]+(?![a-z])|\d+")
_STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "to",
    "of",
    "in",
    "on",
    "for",
    "is",
    "it",
    "be",
    "with",
    "that",
    "this",
    "as",
    "at",
    "by",
    "from",
    "we",
    "i",
    "you",
    "def",
    "self",
    "return",
    "import",
    "if",
    "else",
    "not",
    "none",
    "true",
    "false",
    "class",
    "pass",
    "please",
    "make",
    "should",
    "does",
    "when",
    "what",
    "where",
    "how",
}
# extra noise when the query is matched against code (task words, not code words)
_CODE_STOP = {"fix", "bug", "test", "tests", "broken", "issue", "error", "wrong"}
CODE_EXT = {
    ".py",
    ".js",
    ".mjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".cs",
    ".php",
    ".kt",
    ".swift",
    ".c",
    ".cpp",
    ".h",
    ".sql",
    ".sh",
}


def stem(word: str) -> str:
    """Light suffix stripping so "reserving", "reserved", "reserves" and "reserve" all meet."""
    base = word
    for suffix, keep in (("ing", 4), ("ed", 4), ("es", 4), ("s", 4)):
        if word.endswith(suffix) and len(word) - len(suffix) >= keep:
            base = word[: -len(suffix)]
            if suffix in {"ing", "ed"} and len(base) > 2 and base[-1] == base[-2]:
                base = base[:-1]  # "stopped" → "stop"
            break
    if base.endswith("e") and len(base) > 4:
        base = base[:-1]  # "reserve" → "reserv", the same stem as "reserving"
    return base


def tokens(text: str, *, code: bool = False) -> list[str]:
    stop = _STOP | _CODE_STOP if code else _STOP
    out = []
    for word in re.findall(r"[A-Za-z0-9_]+", text or ""):
        parts = [p.lower() for p in _SPLIT.findall(word)] or [word.lower()]
        for p in parts:
            if len(p) > 1 and p not in stop:
                out.append(stem(p))
    return out


@dataclass
class Chunk:
    path: str
    start: int
    end: int
    text: str


def chunk_file(rel: str, text: str, window: int = 40) -> list[Chunk]:
    lines = text.splitlines()
    if rel.endswith(".py"):
        try:
            tree = ast.parse(text)
            out = []
            for node in tree.body:
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                    s, e = node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno
                    methods = (
                        [
                            m
                            for m in node.body
                            if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)
                        ]
                        if isinstance(node, ast.ClassDef)
                        else []
                    )
                    if len(methods) < 2:
                        out.append(Chunk(rel, s, e, "\n".join(lines[s - 1 : e])))
                        continue
                    # a class with several methods: one chunk per method, each led by the class
                    # line for context, so a long class does not drown in BM25 length norms
                    head = lines[s - 1]
                    first = methods[0].lineno
                    if first - 1 >= s:
                        out.append(Chunk(rel, s, first - 1, "\n".join(lines[s - 1 : first - 1])))
                    for m in methods:
                        ms, me = m.lineno, getattr(m, "end_lineno", m.lineno) or m.lineno
                        body = "\n".join(lines[ms - 1 : me])
                        out.append(Chunk(rel, ms, me, f"{head}\n{body}"))
            if out:
                head_end = min(c.start for c in out) - 1
                if head_end > 0:
                    out.append(Chunk(rel, 1, head_end, "\n".join(lines[:head_end])))
                return out
        except SyntaxError:
            pass
    return [
        Chunk(rel, i + 1, min(len(lines), i + window), "\n".join(lines[i : i + window]))
        for i in range(0, max(1, len(lines)), window)
    ]


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = 0.75) -> None:
        self.docs = docs
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.avgdl = (sum(len(d) for d in docs) / self.n) if self.n else 0.0
        self.tf = [Counter(d) for d in docs]
        df: Counter[str] = Counter()
        for d in docs:
            df.update(set(d))
        self.idf = {t: math.log(1 + (self.n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: list[str]) -> list[float]:
        out = []
        for d, tf in zip(self.docs, self.tf, strict=True):
            s = 0.0
            dl = len(d) or 1
            for t in set(query):
                f = tf.get(t)
                if not f:
                    continue
                s += (
                    self.idf.get(t, 0.0)
                    * f
                    * (self.k1 + 1)
                    / (f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1)))
                )
            out.append(s)
        return out


def _cache_path(root: Path) -> Path:
    return root / ".trendlab" / "index" / "chunks.json"


def index_project(
    root: Path, rules=None, max_files: int = 1500, max_bytes: int = 400_000, *, cache: bool = True
) -> list[Chunk]:
    """Chunks of the project's code files. With ``cache`` (U29 workspace caching), a file whose
    size and modification time are unchanged reuses its chunks from ``.trendlab/index/``."""
    import json as _json

    cached: dict = {}
    if cache:
        try:
            cached = _json.loads(_cache_path(root).read_text())
        except (OSError, ValueError):
            cached = {}
    fresh: dict = {}
    chunks: list[Chunk] = []
    for rel, st, path in _code_files(root, rules, max_files, max_bytes):
        key = f"{st.st_size}:{int(st.st_mtime_ns)}"
        hit = cached.get(rel)
        if hit and hit.get("key") == key:
            file_chunks = [Chunk(rel, c[0], c[1], c[2]) for c in hit["chunks"]]
        else:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            file_chunks = chunk_file(rel, text)
        fresh[rel] = {"key": key, "chunks": [[c.start, c.end, c.text] for c in file_chunks]}
        chunks.extend(file_chunks)
    if cache and fresh != cached:
        try:
            _cache_path(root).parent.mkdir(parents=True, exist_ok=True)
            _cache_path(root).write_text(_json.dumps(fresh))
        except OSError:
            pass
    return chunks


def _code_files(root: Path, rules, max_files: int, max_bytes: int):
    from trendlab.context.ignore import IgnoreRules

    rules = rules or IgnoreRules.for_project(root, respect_gitignore=True)
    for p in rules.walk(root, max_files=max_files):
        if p.suffix.lower() not in CODE_EXT:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size > max_bytes:
            continue
        rel = p.relative_to(root).as_posix()
        if "test" in rel.lower().split("/")[-1]:
            continue  # the code under test is what we want to surface, not the tests
        yield rel, st, p


def search(chunks: list[Chunk], query: str, k: int = 3) -> list[tuple[Chunk, float]]:
    q = tokens(query, code=True)
    if not q or not chunks:
        return []
    bm = BM25([tokens(c.path.replace("/", " ") + " " + c.text, code=True) for c in chunks])
    ranked = sorted(zip(chunks, bm.scores(q), strict=True), key=lambda cs: -cs[1])
    return [(c, round(s, 3)) for c, s in ranked[:k] if s > 0]


def hints_message(hits: list[tuple[Chunk, float]], max_chars: int = 3000) -> str:
    if not hits:
        return ""
    parts = [
        "Retrieved code that may be relevant to this request (a lexical search, not certain — "
        "read the files before relying on it):"
    ]
    used = 0
    for c, _s in hits:
        body = c.text if len(c.text) < 1200 else c.text[:1200] + "\n…"
        block = f"\n{c.path}:{c.start}-{c.end}\n```\n{body}\n```"
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    return "\n".join(parts)
