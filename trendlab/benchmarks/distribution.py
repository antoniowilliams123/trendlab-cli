"""Distribution shift (drift watch): how far real work is from what the suite measures.

Profiles stored sessions (TrendLab's own and imported ones) by language of the files touched,
task kind (fix / feature / question) and prompt length, profiles the suite the same way, and
reports the total-variation distance per dimension plus the biggest gaps. A large distance says
the suite's pass rate is a poor predictor of real-world performance (evaluation realism).
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

EXT_LANG = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",
    ".mjs": "typescript",
    ".jsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".cs": "csharp",
    ".md": "docs",
    ".html": "web",
    ".css": "web",
    ".json": "config",
    ".toml": "config",
    ".yaml": "config",
    ".yml": "config",
    ".sql": "sql",
    ".sh": "shell",
}
_FIX = re.compile(
    r"\b(fix|bug|broken|fail(?:s|ing|ed)?|error|crash|regress|wrong|incorrect)\b", re.I
)
_FEATURE = re.compile(r"\b(add|implement|create|build|support|new|extend|refactor|write)\b", re.I)


def task_kind(prompt: str) -> str:
    if _FIX.search(prompt or ""):
        return "fix"
    if _FEATURE.search(prompt or ""):
        return "feature"
    return "question/other"


def length_bucket(prompt: str) -> str:
    n = len(prompt or "")
    return "short (<150)" if n < 150 else "medium (150-400)" if n < 400 else "long (>400)"


def _norm(c: Counter) -> dict[str, float]:
    total = sum(c.values()) or 1
    return {k: v / total for k, v in c.items()}


def tv_distance(a: Counter, b: Counter) -> float:
    pa, pb = _norm(a), _norm(b)
    keys = set(pa) | set(pb)
    return round(0.5 * sum(abs(pa.get(k, 0.0) - pb.get(k, 0.0)) for k in keys), 3)


def profile_sessions(store, days: int = 30, limit: int = 2000) -> dict[str, Counter]:
    from datetime import UTC, datetime, timedelta

    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    langs: Counter = Counter()
    kinds: Counter = Counter()
    lengths: Counter = Counter()
    for s in store.sessions(limit=limit):
        if s.get("updated_at", "") < since or "/trendlab-suite-" in (s.get("project_path") or ""):
            continue
        first = None
        for m in store.messages(s["id"]):
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                first = m["content"]
                break
        if first is None:
            continue
        kinds[task_kind(first)] += 1
        lengths[length_bucket(first)] += 1
        for e in store.events(s["id"], "tool.started"):
            d = e.get("data") or {}
            for f in list(d.get("files") or []) + [str(d.get("detail") or "")]:
                ext = Path(str(f).split()[0] if f else "").suffix.lower()
                if ext in EXT_LANG:
                    langs[EXT_LANG[ext]] += 1
    return {"lang": langs, "kind": kinds, "length": lengths}


def profile_suite() -> dict[str, Counter]:
    from trendlab.benchmarks.suite import TASKS

    langs: Counter = Counter(t.lang for t in TASKS)
    kinds: Counter = Counter(task_kind(t.prompt) for t in TASKS)
    lengths: Counter = Counter(length_bucket(t.prompt) for t in TASKS)
    return {"lang": langs, "kind": kinds, "length": lengths}


def shift_report(real: dict[str, Counter], suite: dict[str, Counter]) -> dict[str, Any]:
    out: dict[str, Any] = {"dimensions": {}}
    for dim in ("lang", "kind", "length"):
        pr, ps = _norm(real[dim]), _norm(suite[dim])
        gaps = sorted(
            ((k, round(pr.get(k, 0.0) - ps.get(k, 0.0), 3)) for k in set(pr) | set(ps)),
            key=lambda kv: -abs(kv[1]),
        )
        out["dimensions"][dim] = {
            "tv_distance": tv_distance(real[dim], suite[dim]),
            "real": {k: round(v, 3) for k, v in sorted(pr.items(), key=lambda kv: -kv[1])},
            "suite": {k: round(v, 3) for k, v in sorted(ps.items(), key=lambda kv: -kv[1])},
            "biggest_gaps": gaps[:4],
        }
    worst = max(out["dimensions"].items(), key=lambda kv: kv[1]["tv_distance"])
    out["worst_dimension"] = worst[0]
    out["real_sessions"] = sum(real["kind"].values())
    out["verdict"] = (
        "suite matches real work"
        if worst[1]["tv_distance"] < 0.2
        else "suite partly matches real work"
        if worst[1]["tv_distance"] < 0.4
        else "suite does not represent real work on " + worst[0]
    )
    return out
