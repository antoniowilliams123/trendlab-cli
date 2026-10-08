"""Uplift for the six user-felt terms: invented edge cases, paper cuts, plain language,
architecture before implementation."""

import json
from types import SimpleNamespace

from trendlab.agent.planner import design_text, lint_plan, needs_design, parse_steps, plan_message
from trendlab.agent.prompt import _POLICY
from trendlab.agent.style import check, measure
from trendlab.agent.tasks import Plan
from trendlab.benchmarks.runner import _test_sections, design_metrics, reference_fit
from trendlab.benchmarks.suite import get_task


def test_style_scores_jargon_tone_and_simplified_english():
    m = measure(
        "The bug was caused by a typo. Perfect! We streamline the ecosystem. Sorry about that."
    )
    assert m["jargon"] == ["ecosystem", "streamline"]
    assert m["tone"]["hype"] == ["perfect!"] and m["tone"]["apologies"] == ["sorry"]
    assert m["passive_share"] > 0 and m["max_sentence_words"] == 7
    cfg = SimpleNamespace(plain_language=True, max_answer_words=0, tone="calm")
    issues = " | ".join(check(m, cfg))
    assert "buzzwords" in issues and "calm, factual tone" in issues and "apology" in issues
    plain = measure("I changed one line in the parser. The tests pass.")
    assert check(plain, cfg) == [] and plain["jargon"] == [] and plain["passive_share"] == 0
    assert check(m, SimpleNamespace(plain_language=False, max_answer_words=0, tone="")) == []


def test_long_sentence_and_passive_rules():
    long = " ".join(["word"] * 40) + "."
    cfg = SimpleNamespace(plain_language=True, max_answer_words=0, tone="")
    assert any("split it" in i for i in check(measure(long), cfg))
    passive = "The file was changed. The test was added. The bug was fixed."
    assert any("active voice" in i for i in check(measure(passive), cfg))


DESIGNED = json.dumps(
    {
        "design": {
            "approach": "pass the member flag from order_total into tiered_discount",
            "interfaces": ["order_total(items, tax=True, member=False)"],
            "reuse": ["tiered_discount"],
        },
        "steps": [
            {
                "title": "Thread member through",
                "files": ["shop/orders.py", "shop/pricing.py"],
                "done_when": "member gets 15%",
                "validation": "pytest -q",
            },
            {"title": "Run tests", "files": [], "done_when": "green", "validation": "pytest -q"},
        ],
    }
)


def test_plan_design_is_parsed_required_and_shown_first():
    steps = parse_steps(DESIGNED)
    assert steps[0]["_design"]["interfaces"] == ["order_total(items, tax=True, member=False)"]
    assert needs_design(steps) and not lint_plan(steps)
    del steps[0]["_design"]
    assert any("no design" in i for i in lint_plan(steps))
    one = parse_steps(
        '{"steps": [{"title": "Fix sign", "files": ["shop/pricing.py", "tests/test_pricing.py"],'
        ' "done_when": "ok", "validation": "pytest"}]}'
    )
    assert not needs_design(one) and not any("design" in i for i in lint_plan(one))
    plan = Plan()
    from trendlab.agent.planner import apply_steps

    design = parse_steps(DESIGNED)[0]["_design"]
    msg = plan_message(apply_steps(plan, parse_steps(DESIGNED)), design)
    assert msg.index("Design (decided before coding") < msg.index("T-")
    assert "reuse: tiered_discount" in design_text(design)


def test_design_metrics_adherence_and_undeclared_code():
    design = {"approach": "x", "interfaces": ["order_total(items, member=False)"], "reuse": []}
    diff = (
        "--- a/shop/orders.py\n+++ b/shop/orders.py\n"
        "+def order_total(items, tax=True, member=False):\n+def _helper():\n"
        "--- a/tests/test_orders.py\n+++ b/tests/test_orders.py\n+def test_member():\n"
    )
    m = design_metrics(design, diff)
    assert m["design_implemented"] == 1.0 and m["undeclared_new_defs"] == ["_helper"]
    assert design_metrics(None) == {"design_given": False}


def test_reference_fit_flags_tests_for_behaviour_nobody_asked_for():
    task = get_task("ad05-destructive_request")  # chunks() drops the last element
    keeps = (
        "--- a/tests/test_util.py\n+++ b/tests/test_util.py\n@@ -1,3 +1,7 @@\n"
        " from shop.util import chunks, clamp, parse_sku\n \n \n"
        "+def test_chunks_keeps_last():\n+    assert chunks([1, 2, 3], 2) == [[1, 2], [3]]\n+\n+\n"
    )
    invented = (
        "--- a/tests/test_util.py\n+++ b/tests/test_util.py\n@@ -1,3 +1,9 @@\n"
        "+import pytest\n+\n"
        " from shop.util import chunks, clamp, parse_sku\n \n \n"
        "+def test_chunks_rejects_negative():\n+    with pytest.raises(ValueError):\n"
        "+        chunks([1], -1)\n+\n+\n"
    )
    assert reference_fit(task, keeps) == {"reference_fit": True, "fabricated_tests": []}
    bad = reference_fit(task, invented)
    assert bad["reference_fit"] is False
    assert bad["fabricated_tests"] == ["tests/test_util.py::test_chunks_rejects_negative"]
    assert reference_fit(task, "") == {}
    src_only = "--- a/shop/util.py\n+++ b/shop/util.py\n@@ -1 +1 @@\n-x\n+y\n"
    assert _test_sections(src_only + keeps).startswith("--- a/tests/test_util.py")


def test_policy_tells_the_agent_not_to_invent_edge_cases():
    assert "Fix what was asked" in _POLICY and "unless the user asked" in _POLICY
