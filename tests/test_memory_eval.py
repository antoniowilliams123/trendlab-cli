"""Uplift U30: memory learning keeps durable facts and drops run narration."""

from trendlab.benchmarks.memory_eval import CASES, run


async def test_scoring_counts_kept_and_leaked_facts():
    answers = iter(
        [
            "- Tests only run via `make test`.\n- Money must be Decimal.\n- At 14:02 there were 3 errors.",
            "NONE",
            "- CI runs Python 3.11.",
            "- Never touch migrations.\n- Settings live in config/settings.toml.",
        ]
    )

    async def call(messages):
        assert "Run evidence" in messages[0]["content"]
        return next(answers)

    r = await run(call)
    durable = sum(len(c["durable"]) for c in CASES)
    assert r["durable_recall"] == round(5 / durable, 3)
    assert r["ephemeral_leak"] == round(1 / len(CASES), 3)  # the 14:02 narration leaked
    assert r["rows"][1]["facts"] == []
