"""Uplift U31: the offline end-to-end self-test passes every stage."""

import os

from trendlab.benchmarks.selftest import run


async def test_selftest_passes_every_stage(_trendlab_home):
    before = os.environ.get("TRENDLAB_HOME")
    results = await run()
    assert [r["stage"] for r in results] == [
        "agent run",
        "deterministic replay",
        "review ledger",
        "architecture",
        "health",
        "mutation",
        "symbol check",
        "test-gaming check",
    ]
    assert all(r["ok"] for r in results), [r for r in results if not r["ok"]]
    assert os.environ.get("TRENDLAB_HOME") == before  # the temporary home was restored
