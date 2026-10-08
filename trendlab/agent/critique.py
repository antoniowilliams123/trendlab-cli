"""Multi-model design critique (uplift U15).

Several reviewers read the same design or plan: a critic per model, plus an adversary that
argues the design will fail. Their points are merged: a point two or more reviewers raise is
consensus; one raised by a single reviewer is kept as a dissent worth a look. Different models
(or the same model told to attack) miss different things, which is the point of a panel.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

CRITIC = """You review a software design before anyone builds it. Find the problems that would
cost the most later: correctness and data-integrity risks, security holes, failure handling,
scaling limits, missing requirements, and simpler alternatives. Report only real problems with
this design; do not pad.

Answer with ONE JSON object and nothing else:
{{"points": [{{"claim": "the problem in one sentence", "why": "what goes wrong",
  "severity": "high" | "med" | "low", "category": "correctness" | "security" | "reliability"
  | "scaling" | "requirements" | "simplicity"}}]}}

## Design
{doc}
"""

ADVERSARY = """You are the adversary in a design review. Assume this design ships and fails in
production within a month. Argue the most likely ways it fails — concrete scenarios, not
generic worries.

Answer with ONE JSON object and nothing else:
{{"points": [{{"claim": "how it fails, in one sentence", "why": "the scenario",
  "severity": "high" | "med" | "low", "category": "correctness" | "security" | "reliability"
  | "scaling" | "requirements" | "simplicity"}}]}}

## Design
{doc}
"""

_JSON = re.compile(r"\{.*\}", re.S)
_WORD = re.compile(r"[a-z][a-z0-9]{2,}")
_STOP = set("the and for that this with will are not but can from have has its into when".split())


def parse_points(text: str) -> list[dict[str, Any]] | None:
    m = _JSON.search(text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(obj.get("points"), list):
        return None
    out = []
    for p in obj["points"]:
        if isinstance(p, dict) and str(p.get("claim") or "").strip():
            sev = str(p.get("severity") or "med").lower()
            out.append(
                {
                    "claim": str(p["claim"]).strip()[:300],
                    "why": str(p.get("why") or "").strip()[:500],
                    "severity": sev if sev in {"high", "med", "low"} else "med",
                    "category": str(p.get("category") or "correctness").lower()[:20],
                }
            )
    return out


def _words(p: dict[str, Any]) -> set[str]:
    return {w for w in _WORD.findall((p["claim"] + " " + p["why"]).lower()) if w not in _STOP}


def merge(per_reviewer: dict[str, list[dict[str, Any]]], threshold: float = 0.25):
    """Cluster similar points across reviewers (word overlap); consensus = 2+ reviewers."""
    clusters: list[dict[str, Any]] = []
    for reviewer, points in per_reviewer.items():
        for p in points:
            w = _words(p)
            best, score = None, 0.0
            for c in clusters:
                jac = len(w & c["words"]) / max(1, len(w | c["words"]))
                if jac > score:
                    best, score = c, jac
            if best is not None and score >= threshold and reviewer not in best["reviewers"]:
                best["reviewers"].append(reviewer)
                best["words"] |= w
                if {"high": 0, "med": 1, "low": 2}[p["severity"]] < {"high": 0, "med": 1, "low": 2}[
                    best["severity"]
                ]:
                    best["severity"] = p["severity"]
            else:
                clusters.append({**p, "reviewers": [reviewer], "words": set(w)})
    for c in clusters:
        c["consensus"] = len(c["reviewers"]) >= 2
        c.pop("words")
    order = {"high": 0, "med": 1, "low": 2}
    clusters.sort(key=lambda c: (not c["consensus"], order[c["severity"]]))
    return clusters


Call = Callable[[list[dict[str, Any]]], Awaitable[str]]


async def critique(
    doc: str, reviewers: dict[str, Call], *, adversary: Call | None = None
) -> dict[str, Any]:
    from trendlab.providers.structured_json import ask_json

    async def one(call, prompt):
        got, _ = await ask_json(call, [{"role": "user", "content": prompt}], parse_points)
        return got or []

    names = list(reviewers)
    jobs = [one(reviewers[n], CRITIC.format(doc=doc[:30_000])) for n in names]
    if adversary is not None:
        names.append("adversary")
        jobs.append(one(adversary, ADVERSARY.format(doc=doc[:30_000])))
    results = await asyncio.gather(*jobs)
    per = dict(zip(names, results, strict=True))
    points = merge(per)
    return {
        "reviewers": names,
        "points": points,
        "consensus": sum(1 for p in points if p["consensus"]),
        "per_reviewer": {n: len(v) for n, v in per.items()},
        "raw": per,
    }
