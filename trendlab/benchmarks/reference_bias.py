"""Reference-bias probe (uplift U30): does the verifier favour the reference fix's wording?

For each task the verifier judges two correct fixes of the same bug: the reference fix, and an
equivalent fix written differently. Both make the hidden test pass (checked here, so the probe
never compares a correct fix with a wrong one). An unbiased verifier passes them equally often.
"""

from __future__ import annotations

import difflib
import subprocess
import tempfile
from pathlib import Path
from typing import Any

# task id -> an equivalent correct replacement for the buggy snippet (defect.new)
ALTERNATIVES = {
    "py01-off_by_one": "range(0, len(items) if items else 0, size)",
    "py13-swapped_args": "return low if value < low else high if value > high else value",
    "py10-wrong_operator": "if not n > threshold",
    "py15-wrong_operator": "    if not total < 100:\n        return 0.05",
    "py03-missing_none_check": '        if int(item.get("qty") or 0) <= 0:',
    "py06-early_return": "        self.levels[sku] = self.levels.get(sku, 0) - qty\n        return self.levels[sku]",
}


def fixes(task) -> tuple[str, str, str]:
    """(buggy file, reference-fixed file, alternative-fixed file) for the defect's file."""
    from trendlab.benchmarks import suite as suite_mod

    ref = suite_mod.BASES[task.lang][task.defect.file]
    buggy = ref.replace(task.defect.old, task.defect.new, 1)
    alt = buggy.replace(task.defect.new, ALTERNATIVES[task.id], 1)
    return buggy, ref, alt


def passes_hidden_test(task, content: str) -> bool:
    from trendlab.benchmarks import suite as suite_mod

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "r"
        suite_mod.materialize(task, root)
        (root / task.defect.file).write_text(content)
        suite_mod.write_hidden_test(task, root)
        return (
            subprocess.run(task.test_command, shell=True, cwd=root, capture_output=True).returncode
            == 0
        )


def _diff(a: str, b: str, rel: str) -> str:
    return "".join(
        difflib.unified_diff(a.splitlines(True), b.splitlines(True), f"a/{rel}", f"b/{rel}", n=6)
    )


async def probe(call, tasks, *, repeats: int = 3) -> dict[str, Any]:
    from trendlab.agent.verifier import verify

    rows = []
    for t in tasks:
        buggy, ref, alt = fixes(t)
        assert passes_hidden_test(t, ref) and passes_hidden_test(t, alt), t.id
        scores = {}
        for label, fixed in (("reference", ref), ("alternative", alt)):
            diff = _diff(buggy, fixed, t.defect.file)
            ok = 0
            for _ in range(repeats):
                v = await verify(call, task=t.prompt, diff=diff, validation=None, plan="")
                ok += bool(v and v.verdict == "pass")
            scores[label] = ok
        rows.append({"task": t.id, **scores})
    n = len(rows) * repeats
    ref_rate = sum(r["reference"] for r in rows) / n
    alt_rate = sum(r["alternative"] for r in rows) / n
    return {
        "tasks": len(rows),
        "repeats": repeats,
        "reference_pass": round(ref_rate, 3),
        "alternative_pass": round(alt_rate, 3),
        "gap": round(ref_rate - alt_rate, 3),
        "rows": rows,
    }
