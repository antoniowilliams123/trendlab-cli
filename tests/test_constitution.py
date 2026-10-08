"""The agent constitution only lists rules that are enforced, proven and measured (U23)."""

import ast
import importlib
import re
from pathlib import Path

from trendlab.benchmarks.runner import summarize
from trendlab.benchmarks.suite import TASKS

DOC = Path(__file__).resolve().parent.parent / "docs" / "AGENT_CONSTITUTION.md"
ROW = re.compile(r"^\| (C\d+) \| (.+?) \| `([\w.]+)` \| `([\w/.]+)::(\w+)` \| (.+?) \|$", re.M)


def test_every_rule_points_at_real_code_and_a_real_test():
    rows = ROW.findall(DOC.read_text())
    assert len(rows) == 14
    for cid, _rule, symbol, test_file, test_name, _metric in rows:
        module, _, attr = symbol.rpartition(".")
        assert hasattr(importlib.import_module(module), attr), f"{cid}: {symbol} is gone"
        tree = ast.parse((DOC.parent.parent / test_file).read_text())
        names = {
            n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        assert test_name in names, f"{cid}: {test_file}::{test_name} is gone"


def test_sycophancy_tier_is_measured():
    syc = [t for t in TASKS if t.tier == "sycophancy"]
    assert len(syc) == 5 and all(
        t.misleading_file and t.misleading_file != t.defect.file for t in syc
    )
    assert all(t.misleading_file in t.prompt for t in syc)
    rows = [
        {
            "task": "a",
            "passes": True,
            "cost": 0.0,
            "status": "COMPLETED",
            "followed_wrong_hint": False,
        },
        {
            "task": "b",
            "passes": False,
            "cost": 0.0,
            "status": "COMPLETED",
            "followed_wrong_hint": True,
        },
    ]
    s = summarize(rows)["sycophancy"]
    assert s == {"tasks": 2, "followed_wrong_hint": 0.5, "fixed_real_cause": 0.5}


def test_proactivity_rates():
    rows = [
        {
            "task": "q1",
            "passes": True,
            "cost": 0.0,
            "status": "COMPLETED",
            "edited_on_question": False,
        },
        {
            "task": "q2",
            "passes": False,
            "cost": 0.0,
            "status": "COMPLETED",
            "edited_on_question": True,
        },
        {"task": "f1", "passes": False, "cost": 0.0, "status": "COMPLETED", "idle_on_fix": True},
        {
            "task": "f2",
            "passes": True,
            "cost": 0.0,
            "status": "COMPLETED",
            "idle_on_fix": False,
            "questions_asked": 0,
        },
    ]
    p = summarize(rows)["proactivity"]
    assert p == {"overreach_rate": 0.5, "timidity_rate": 0.5}
