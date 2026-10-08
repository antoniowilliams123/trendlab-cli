"""Uplift U25: mutation testing finds behaviour the tests do not pin down."""

from pathlib import Path

from trendlab.agent.mutation import mutants, run

SRC = """def tier(total):
    if total >= 100:
        return 0.05
    return 0.0


def add(a, b):
    return a + b
"""


def test_mutants_cover_the_classic_operators():
    kinds = {m.kind for m in mutants(SRC)}
    assert {"comparison", "constant", "arithmetic", "early return"} <= kinds
    only = mutants(SRC, functions={"add"})
    assert only and all(m.line >= 7 for m in only)
    assert all(m.source != SRC for m in mutants(SRC))


def test_run_kills_tested_behaviour_and_reports_survivors(tmp_path: Path):
    (tmp_path / "m.py").write_text(SRC)
    (tmp_path / "test_m.py").write_text(
        "from m import add, tier\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\n"
        "def test_tier():\n    assert tier(50) == 0.0 and tier(200) == 0.05\n"
    )
    r = run(tmp_path, "m.py", "python3 -m pytest -q -x -p no:cacheprovider")
    assert (tmp_path / "m.py").read_text() == SRC  # always restored
    assert r["mutants"] == 4 and 0 < r["killed"] < r["mutants"]
    # nothing tests a total of exactly 100: the >= → > flip survives
    assert {"line": 2, "kind": "comparison", "change": "GtE → Gt"} in r["survivors"]
