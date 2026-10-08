"""Judge quality (uplift U2): pairwise verdicts with position swap, and judge accuracy.

A model used as a judge has known biases — it prefers the first answer, the longer one, the
prettier one. This module makes those measurable: pairwise comparisons are asked twice with
the order swapped and only count when both orders agree; verifier verdicts are scored against
hidden-test ground truth in the benchmark so a verifier model earns a precision/recall of its
own instead of blind trust.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

PAIRWISE_PROMPT = """You compare two candidate changes for the same task. Judge only correctness,
minimality and safety of the change. Ignore length, formatting and writing style; a longer or
more elaborate change is not a better one.

Answer with ONE JSON object: {{"winner": "A" | "B" | "tie", "reason": "one sentence"}}

## Task
{task}

## Candidate A
{a}

## Candidate B
{b}
"""
_JSON = re.compile(r"\{.*\}", re.S)
MAX_DIFF = 20_000


def parse_pairwise(text: str) -> str | None:
    m = _JSON.search(text or "")
    if not m:
        return None
    try:
        w = str(json.loads(m.group(0)).get("winner") or "").strip().upper()
    except ValueError:
        return None
    return w if w in {"A", "B", "TIE"} else None


async def pairwise(
    call: Callable[[list[dict[str, Any]]], Awaitable[str]], *, task: str, a: str, b: str
) -> dict[str, Any]:
    """Ask twice with the order swapped. ``winner`` is "a", "b" or "tie"; ``position_bias`` is
    True when the two orders disagreed (then the result is a tie)."""

    def msgs(x: str, y: str) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": PAIRWISE_PROMPT.format(
                    task=task.strip()[:4000], a=x[:MAX_DIFF], b=y[:MAX_DIFF]
                ),
            }
        ]

    first = parse_pairwise(await call(msgs(a, b)))
    second = parse_pairwise(await call(msgs(b, a)))
    if first is None or second is None:
        return {"winner": "tie", "position_bias": False, "orders": [first, second], "usable": False}
    # map the swapped order back: in the second call "A" was candidate b
    second_mapped = {"A": "B", "B": "A", "TIE": "TIE"}[second]
    if first == second_mapped:
        return {
            "winner": {"A": "a", "B": "b", "TIE": "tie"}[first],
            "position_bias": False,
            "orders": [first, second],
            "usable": True,
        }
    return {"winner": "tie", "position_bias": True, "orders": [first, second], "usable": True}


def judge_accuracy(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Verifier verdicts vs hidden-test ground truth over benchmark rows (U2).

    - ``pass_precision``: of changes the verifier passed, the share that really passed;
    - ``false_pass_rate``: of changes that really failed, the share the verifier passed (misses);
    - ``false_flag_rate``: of changes that really passed, the share the verifier flagged.
    """
    judged = [r for r in rows if r.get("verification") in {"pass", "fix", "fail"} and "passes" in r]
    if not judged:
        return {"judged": 0}
    passed_by_judge = [r for r in judged if r["verification"] == "pass"]
    truly_failed = [r for r in judged if not r["passes"]]
    truly_passed = [r for r in judged if r["passes"]]
    out = {"judged": len(judged)}
    out["pass_precision"] = (
        round(sum(1 for r in passed_by_judge if r["passes"]) / len(passed_by_judge), 3)
        if passed_by_judge
        else None
    )
    out["false_pass_rate"] = (
        round(sum(1 for r in truly_failed if r["verification"] == "pass") / len(truly_failed), 3)
        if truly_failed
        else None
    )
    out["false_flag_rate"] = (
        round(sum(1 for r in truly_passed if r["verification"] != "pass") / len(truly_passed), 3)
        if truly_passed
        else None
    )
    return out
