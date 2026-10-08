"""Uplift U22: facts survive compaction, including the no-model fallback."""

from trendlab.benchmarks.compaction_eval import SCENARIOS, run, transcript
from trendlab.context.compaction import deterministic_summary


def _oracle():
    """Answers by finding the question's keys in the context — a perfect reader."""

    async def answer(messages):
        text = messages[0]["content"]
        ctx = text.split("## Context", 1)[1].split("## Question", 1)[0].lower()
        q = text.split("## Question", 1)[1].strip()
        for sc in SCENARIOS:
            if sc["task"].lower() not in ctx:  # questions repeat across scenarios
                continue
            for _kind, _t, question, keys in sc["facts"]:
                if question == q:
                    hit = [k for k in keys if k.lower() in ctx]
                    return " ".join(hit) if hit else "unknown"
        return "unknown"

    return answer


async def test_fallback_summary_keeps_every_planted_fact():
    res = await run(None, _oracle(), method="deterministic")
    assert res["retention"] == 1.0 and res["compression"] < 0.15


def test_fallback_keeps_decisions_notes_and_errors_but_not_noise():
    msgs = transcript(SCENARIOS[0])
    summary = deterministic_summary(msgs[:-4], {"changed_files": ["shop/orders.py"]})
    assert "never edit files under legacy/" in summary  # user decision
    assert "AssertionError: 30.0 != 90.0" in summary  # exact error
    assert "compute(" not in summary  # tool-output noise is not copied
    assert len(summary) <= 8000
