"""Uplift U30: contamination probe and reference-bias probe."""

from trendlab.benchmarks.contamination import probe as contamination
from trendlab.benchmarks.reference_bias import ALTERNATIVES, fixes, passes_hidden_test
from trendlab.benchmarks.reference_bias import probe as reference_bias
from trendlab.benchmarks.suite import get_task


async def test_contamination_probe_tells_memorised_from_plausible():
    task = get_task("py01-off_by_one")

    async def memorised(messages):
        return "```python\n" + task.defect.hidden_test + "```"

    async def plausible(messages):
        return (
            "```python\ndef test_chunks():\n    assert chunks([1, 2, 3], 2) == [[1, 2], [3]]\n```"
        )

    m = await contamination(memorised, [task])
    p = await contamination(plausible, [task])
    assert m["fully_reproduced"] == 1 and m["mean_similarity"] == 1.0
    assert p["fully_reproduced"] == 0 and p["mean_similarity"] < 0.6


def test_alternative_fixes_are_correct_and_different():
    for tid in ALTERNATIVES:
        t = get_task(tid)
        buggy, ref, alt = fixes(t)
        assert alt != ref and passes_hidden_test(t, alt) and not passes_hidden_test(t, buggy)


async def test_reference_bias_gap():
    async def biased(messages):
        text = messages[0]["content"]
        verdict = "pass" if "range(0, len(items), size)" in text else "fix"
        return f'{{"verdict": "{verdict}", "findings": []}}', "m"

    r = await reference_bias(biased, [get_task("py01-off_by_one")], repeats=2)
    assert r["reference_pass"] == 1.0 and r["alternative_pass"] == 0.0 and r["gap"] == 1.0
