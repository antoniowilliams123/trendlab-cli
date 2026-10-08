"""Uplift U1: intervals, paired significance, pass@k, tool reliability, release gate, canary."""

from datetime import UTC, datetime
from pathlib import Path

from trendlab.benchmarks import suite as suite_mod
from trendlab.benchmarks.runner import CANARY, compare_rows, run_suite, select_tasks, summarize
from trendlab.benchmarks.stats import (
    bootstrap_ci,
    exact_sign_test,
    gate,
    paired_outcomes,
    pass_at_k,
    wilson_interval,
)
from trendlab.config.schema import AppConfig, ProviderConfig
from trendlab.engine.daemon import Engine
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider


def _rows(task_pass: dict[str, list[bool]], cost=0.01):
    out = []
    for t, runs in task_pass.items():
        for i, p in enumerate(runs):
            out.append(
                {
                    "task": t,
                    "passes": p,
                    "status": "COMPLETED",
                    "cost": cost,
                    "run": i + 1,
                    "tool_calls": 4,
                    "tool_failures": 0 if p else 1,
                }
            )
    return out


def test_intervals_and_sign_test():
    assert wilson_interval(0, 0) == (0.0, 0.0)
    lo, hi = wilson_interval(40, 40)
    assert lo > 0.9 and hi == 1.0  # 40/40 is not "certainly 100 %"
    lo10, _ = wilson_interval(10, 10)
    assert lo10 < lo  # fewer tasks → wider interval
    assert exact_sign_test(0, 0) == 1.0
    assert exact_sign_test(1, 0) == 1.0  # one task cannot be significant
    assert exact_sign_test(6, 0) < 0.05 and exact_sign_test(3, 3) == 1.0
    lo, hi = bootstrap_ci([1.0, 1.0, 1.0])
    assert (lo, hi) == (1.0, 1.0)
    lo, hi = bootstrap_ci([0.0, 1.0] * 10)
    assert lo < 0.5 < hi


def test_pass_at_k_flakiness_and_paired_outcomes():
    rows = _rows({"a": [True, False, True], "b": [True, True, True], "c": [False, False, False]})
    pk = pass_at_k(rows)
    assert pk == {
        "pass_at_1": round((2 / 3 + 1 + 0) / 3, 3),
        "pass_at_k": round(2 / 3, 3),
        "k": 3,
        "flaky_tasks": 1,
    }
    a = _rows({"a": [True], "b": [True], "c": [False], "d": [True]})
    b = _rows({"a": [True], "b": [False], "c": [True], "d": [False]})
    paired = paired_outcomes(a, b, "passes")
    assert paired["b_wins"] == 1 and paired["b_losses"] == 2 and paired["ties"] == 1
    assert paired["tasks_only_a_passes"] == ["b", "d"] and paired["p_value"] == 1.0
    g = gate(paired, cost_ratio=1.2)
    assert g["ok"]  # not significant → cannot fail the gate on quality
    g2 = gate({"b_wins": 0, "b_losses": 7, "p_value": 0.0156}, cost_ratio=1.0)
    assert not g2["ok"] and "loses 7" in g2["reasons"][0]
    g3 = gate(paired, cost_ratio=2.0)
    assert not g3["ok"] and "2.00×" in g3["reasons"][0]


def test_summarize_and_compare_rows_report_rigour():
    a = _rows({t: [True] for t in "abcdefghij"}, cost=0.01)
    b = _rows({t: [True] for t in "abcdefghi"} | {"j": [False]}, cost=0.013)
    sa = summarize(a)
    assert sa["passes_ci95"][0] < 1.0 and sa["tool_success_rate"] == 1.0 and sa["error_rate"] == 0.0
    cmp = compare_rows(a, b)
    assert cmp["paired_passes"]["b_losses"] == 1 and cmp["paired_passes"]["p_value"] == 1.0
    assert cmp["cost_ratio"] == 1.3 and cmp["gate"]["ok"]
    assert summarize(b)["error_rate"] == 0.1 and summarize(b)["tool_success_rate"] < 1.0


async def test_run_suite_repeats_tasks_and_counts_tools(_trendlab_home: Path):
    task = suite_mod.get_task("py01-off_by_one")
    responses = [
        ModelResponse(tool_calls=[ToolCall(id="1", name="run_tests", arguments={"kind": "test"})]),
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="2",
                    name="patch_file",
                    arguments={
                        "path": "shop/util.py",
                        "old_text": "len(items) - 1, size",
                        "new_text": "len(items), size",
                    },
                )
            ]
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="3",
                    name="write_file",
                    arguments={
                        "path": "tests/test_regress.py",
                        "content": "from shop.util import chunks\n\n\ndef test_tail():\n    assert chunks([1, 2, 3], 2) == [[1, 2], [3]]\n",
                    },
                )
            ]
        ),
        ModelResponse(tool_calls=[ToolCall(id="4", name="run_tests", arguments={"kind": "test"})]),
        ModelResponse(text="Fixed the off-by-one in chunks(); tests pass."),
    ]
    provider = ScriptedProvider(responses * 2)
    cfg = AppConfig(providers={"scripted": ProviderConfig()})
    cfg.remote_approval.enabled = False
    rows = await run_suite(
        "scripted:m",
        profile="bare",
        selector=task.id,
        runs=2,
        config=cfg,
        provider=provider,
        home=_trendlab_home,
    )
    assert [r["run"] for r in rows] == [1, 2] and all(r["passes"] for r in rows)
    assert rows[0]["tool_calls"] == 4 and rows[0]["tool_failures"] == 0
    assert rows[0]["tools"]["run_tests"]["calls"] == 2 and rows[0]["model_version"] == "scripted:m"
    s = summarize(rows)
    assert s["k"] == 2 and s["flaky_tasks"] == 0 and s["pass_at_k"] == 1.0
    assert len(select_tasks(CANARY, None, "all")) == 10


async def test_canary_schedule(tmp_path: Path):
    cfg = AppConfig()
    cfg.engine.canary = True
    cfg.engine.canary_at = "03:30"
    eng = Engine(projects=[tmp_path], config=cfg, tick_seconds=0.05)
    eng.started -= 1000
    assert eng.due("canary", datetime(2026, 10, 8, 4, 0, tzinfo=UTC))
    assert not eng.due("canary", datetime(2026, 10, 8, 3, 0, tzinfo=UTC))
    cfg.engine.canary = False
    assert not eng.due("canary", datetime(2026, 10, 8, 4, 0, tzinfo=UTC))
    eng.inbox.close()
