"""Uplift U19: specification traceability — requirements, evidence-checked status, drift."""

import json
from pathlib import Path

from trendlab.agent.spec import (
    SHOP_SPEC,
    SHOP_TRUTH,
    check,
    drift,
    extract,
    listed_requirements,
    parse_results,
)
from trendlab.benchmarks import suite as suite_mod


def test_listed_specs_need_no_model_call():
    reqs = listed_requirements(SHOP_SPEC)
    assert len(reqs) == 10 and reqs[0] == {
        "id": "R1",
        "text": "Orders of 100 or more get 5% off and orders of 500 or more get 10% off.",
    }
    assert set(SHOP_TRUTH) == {r["id"] for r in reqs}


async def test_prose_specs_are_extracted_by_the_model():
    async def call(messages):
        return '{"requirements": [{"id": "A", "text": "users can reset passwords by email"}]}'

    reqs = await extract(call, "Users should be able to reset their password via an emailed link.")
    assert reqs == [{"id": "A", "text": "users can reset passwords by email"}]


def test_parse_results_normalises():
    got = parse_results(
        '{"results": [{"id": "R1", "status": "Done", "tested": 1, '
        '"evidence": ["a.py:1"]}, {"status": "missing"}]}'
    )
    assert got == [
        {"id": "R1", "status": "missing", "tested": True, "evidence": ["a.py:1"], "note": ""}
    ]


async def test_check_verifies_evidence_and_drift(tmp_path: Path):
    root = tmp_path / "repo"
    suite_mod.materialize(suite_mod.get_task("py01-off_by_one"), root)
    reqs = listed_requirements(SHOP_SPEC)[:2]
    seen = []

    async def call(messages):
        seen.append(messages[0]["content"])
        return json.dumps(
            {
                "results": [
                    {
                        "id": "R1",
                        "status": "implemented",
                        "tested": True,
                        "evidence": ["shop/pricing.py:20", "shop/pricing.py:999"],
                    },
                    {"id": "R2", "status": "partial", "tested": False, "evidence": []},
                ]
            }
        )

    res = await check(call, root, reqs)
    assert res["implemented"] == 1 and res["partial"] == 1 and res["coverage"] == 0.5
    r1 = res["results"][0]
    assert r1["evidence_cited"] == 2 and r1["evidence_verified"] == 1  # line 999 does not exist
    assert "def tiered_discount" in seen[0] and "# tests/" in seen[0]  # code and tests shown
    later = {**res, "results": [{**res["results"][0], "status": "missing"}, res["results"][1]]}
    assert drift(res, later) == [
        {"id": "R1", "text": reqs[0]["text"], "was": "implemented", "now": "missing"}
    ]
    assert drift(None, res) == []
