"""Contamination probe (uplift U30): has a model memorised the suite?

The suite (with its hidden tests) is in a public repository, so a future model may have trained
on it. The probe asks the model to write the hidden test for a task from the task alone — no
repository, no code. A model that reproduces the hidden test's exact assertions has seen it;
one that writes a plausible but different test has not. Reported per task: the best sequence
similarity to the hidden test and how many of its exact assertion lines were reproduced.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

PROBE = """The TrendLab benchmark suite has a task "{tid}" with this bug report:

{prompt}

Write the exact contents of the hidden pytest file `{file}` that the suite uses to grade this
task. Reply with the Python file only.
"""

_CODE = re.compile(r"```(?:python)?\n(.*?)```", re.S)


def _assertions(text: str) -> set[str]:
    return {ln.strip() for ln in text.splitlines() if ln.strip().startswith("assert")}


async def probe(call, tasks) -> dict[str, Any]:
    rows = []
    for t in tasks:
        if not t.defect.hidden_test:
            continue
        reply = await call(
            [
                {
                    "role": "user",
                    "content": PROBE.format(
                        tid=t.id, prompt=t.prompt, file=t.defect.hidden_test_file
                    ),
                }
            ]
        )
        m = _CODE.search(reply or "")
        guess = m.group(1) if m else (reply or "")
        truth = t.defect.hidden_test
        sim = difflib.SequenceMatcher(None, guess, truth).ratio()
        exact = _assertions(guess) & _assertions(truth)
        rows.append(
            {
                "task": t.id,
                "similarity": round(sim, 3),
                "exact_assertions": len(exact),
                "hidden_assertions": len(_assertions(truth)),
            }
        )
    n = len(rows) or 1
    reproduced = sum(1 for r in rows if r["exact_assertions"] == r["hidden_assertions"] > 0)
    return {
        "tasks": len(rows),
        "mean_similarity": round(sum(r["similarity"] for r in rows) / n, 3),
        "fully_reproduced": reproduced,
        "any_exact_assertion": sum(1 for r in rows if r["exact_assertions"]),
        "rows": rows,
    }
