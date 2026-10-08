"""Router (uplift: router model, semantic routing, model selection policy).

Classifies the user's request by meaning before the run starts and sets the run's machinery to
match, so heavy steps are spent only where they pay:

- ``question``  → nothing should change: no planner, review irrelevant;
- ``small_fix`` → defaults (the planner's own heuristic still applies; small changes are
                   reviewed by the session model);
- ``feature``   → plan the steps first for any brief longer than a line (150 chars);
- ``risky``     → deletion, security, credentials, data loss, migrations, payments: every change
                   gets the stronger model's review.

With ``[routing] router`` set, a cheap model classifies (semantic routing). Without it, a rule
router uses the same categories from keywords. Per-run settings are restored when the run ends.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

KINDS = ("question", "small_fix", "feature", "risky")
PROMPT = """Classify the user's request to a coding agent. Answer with ONE JSON object:
{{"kind": "question" | "small_fix" | "feature" | "risky", "confidence": 0.0-1.0, "reason": "short"}}

question  = asks about the code or wants an explanation; nothing should be edited
small_fix = a bug or small change, likely in one or two places
feature   = new behaviour, several files, or a multi-step change
risky     = deletes data or files, touches security, credentials, auth, payments, migrations,
            production config, or anything hard to undo

Request:
{prompt}
"""
_JSON = re.compile(r"\{.*\}", re.S)
_RISKY = re.compile(
    r"\b(delete|drop|wipe|purge|truncate|migrat\w*|password|credential|secret|"
    r"(api|access|auth|bearer|refresh)[ _-]?(key|token)s?|auth|authenticat\w*|authoriz\w*|"
    r"payment|billing|prod(uction)?|rm\s+-rf|force[- ]push|rewrite history)\b",
    re.I,
)
_QUESTION = re.compile(
    r"^\s*(what|where|why|how|which|who|when|does|is|are|can|could|explain|summari[sz]e|list|"
    r"describe|tell me)\b|\?\s*$",
    re.I,
)
_FIX = re.compile(
    r"\b(fix|bug|broken|breaks?|fail(?:s|ing|ed)?|crash\w*|wrong|incorrect|regress\w*|"
    r"drops?|misses|off by one|too (high|low|many|few)|doesn'?t work|not working)\b",
    re.I,
)
_IMPERATIVE_FEATURE = re.compile(
    r"^\s*(please\s+)?(add|implement|build|create|support|introduce|write|refactor|extend|"
    r"design|integrate)\b",
    re.I,
)
_FEATURE = re.compile(
    r"\b(add|implement|create|build|support|new|extend|refactor|introduce|migrate)\b", re.I
)


@dataclass
class Route:
    kind: str
    confidence: float
    reason: str
    by: str  # "model" | "rules"


def rule_route(prompt: str) -> Route:
    p = prompt or ""
    if _RISKY.search(p):
        return Route("risky", 0.6, "mentions an irreversible or sensitive area", "rules")
    if _QUESTION.search(p) and not re.search(r"\b(fix|change|update|make)\b", p, re.I):
        return Route("question", 0.6, "phrased as a question", "rules")
    if _FIX.search(p):  # "fix X and add a regression test" is a fix, not a feature
        return Route("small_fix", 0.6, "reports a defect", "rules")
    if _IMPERATIVE_FEATURE.search(p) or (_FEATURE.search(p) and len(p) > 60):
        return Route("feature", 0.5, "asks for new behaviour", "rules")
    return Route("small_fix", 0.4, "default", "rules")


def parse_route(text: str) -> Route | None:
    m = _JSON.search(text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    kind = str(obj.get("kind") or "").strip()
    if kind not in KINDS:
        return None
    conf = obj.get("confidence")
    conf = float(conf) if isinstance(conf, int | float) and 0 <= float(conf) <= 1 else 0.5
    return Route(kind, round(conf, 3), str(obj.get("reason") or "")[:160], "model")


async def model_route(call: Callable[[list[dict[str, Any]]], Awaitable[str]], prompt: str) -> Route:
    from trendlab.providers.structured_json import ask_json

    route, _ = await ask_json(
        call,
        [{"role": "user", "content": PROMPT.format(prompt=(prompt or "")[:3000])}],
        parse_route,
    )
    if route is None:
        return rule_route(prompt)
    # a model can miss a risky keyword; the rule router is a floor for safety
    if route.kind != "risky" and rule_route(prompt).kind == "risky":
        return Route("risky", route.confidence, route.reason + " (+ sensitive keywords)", "model")
    return route


def settings_for(route: Route) -> dict[str, Any]:
    """Agent attributes to set for this run (restored afterwards)."""
    if route.kind == "question":
        return {"planner_min_chars": 10**9}
    if route.kind == "small_fix":
        return {}  # defaults: the planner's own length/file heuristic still applies
    if route.kind == "feature":
        # plan multi-sentence feature briefs, not one-liners: planning tiny requests cost more
        # than it saved in the M1 measurement
        return {"planner_min_chars": 150}
    return {"verify_min_diff_lines": 0, "verify_min_files": 1}  # risky: strong review always
