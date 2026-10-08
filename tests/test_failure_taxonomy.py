"""Uplift U8: one failure taxonomy, failure-mode table, root-cause analysis."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from trendlab.agent.recovery import FailureClass
from trendlab.agent.taxonomy import CODES, by_code, classify_row, classify_stop, fmea, rca
from trendlab.benchmarks.runner import summarize
from trendlab.cli import app
from trendlab.config.loader import trendlab_home
from trendlab.sessions.store import SessionStore

# One sample per stop path the runtime can produce (runtime.py `stop_reason = ...` sites,
# _limit_hit, _observe, _check_invariants, _verify_before_surface, provider errors).
RUNTIME_STOPS = {
    "max_iterations (40) reached": "BUDGET_ITERATIONS",
    "cost limit $0.50 reached": "BUDGET_COST",
    "max_model_calls (80) reached": "BUDGET_CALLS",
    "wall-clock limit (30 min) reached": "BUDGET_TIME",
    "canceled by user": "CANCELED",
    "invariant violated: changed file missing from disk": "INVARIANT_BROKEN",
    "context overflow after compaction": "CONTEXT_OVERFLOW",
    "plan rejected by user: wrong module": "PLAN_REJECTED",
    "NO_PROGRESS: the same run_tests failure recurred 3 times": "NO_PROGRESS",
    "verifier rejected the change: x should stay 1": "VERIFIER_REJECTED",
}


def test_every_runtime_stop_path_has_a_code():
    for text, code in RUNTIME_STOPS.items():
        assert classify_stop("FAILED", text) == code, text
    # provider failures are stopped as "<FailureClass>: <error>"
    for fc in FailureClass:
        if fc in {
            FailureClass.TEST_FAILURE,
            FailureClass.PATCH_CONFLICT,
            FailureClass.PERMISSION_DENIED,
        }:
            continue  # recovered inside the loop, never a stop reason
        got = classify_stop("FAILED", f"{fc.value}: boom")
        assert got != "UNKNOWN", fc
    assert classify_stop("COMPLETED", "anything") is None
    assert classify_stop("CANCELED", None) == "CANCELED"
    assert classify_stop("FAILED", "something new") == "UNKNOWN"


def test_runtime_stop_strings_are_all_covered():
    """Guard against a new stop path landing without a code: every literal stop-reason prefix
    in the runtime must classify."""
    src = Path("trendlab/agent/runtime.py").read_text()
    prefixes = set(
        re.findall(r'return f?"(max_iterations|cost limit|max_model_calls|wall-clock)', src)
    )
    prefixes |= set(
        re.findall(r'f?"(NO_PROGRESS|plan rejected|invariant violated|verifier rejected)', src)
    )
    assert len(prefixes) == 8
    for p in prefixes:
        assert classify_stop("FAILED", p + " x") != "UNKNOWN", p


def test_every_code_is_complete():
    for c in CODES.values():
        assert 1 <= c.severity <= 10 and 1 <= c.detection <= 10
        assert c.summary and c.category
        if c.code not in {"CANCELED"}:
            assert c.causes and c.remedies, c.code


def test_bench_rows_get_codes_and_summary_counts():
    base = {
        "status": "COMPLETED",
        "safe": True,
        "passes": True,
        "no_collateral": True,
        "regression_added": True,
        "answered": None,
        "cost": 0.0,
    }
    assert classify_row(base) is None
    assert classify_row({**base, "safe": False, "passes": False}) == "UNSAFE_ACTION"
    assert classify_row({**base, "passes": False}) == "WRONG_FIX"
    assert classify_row({**base, "no_collateral": False}) == "COLLATERAL_DAMAGE"
    assert classify_row({**base, "regression_added": False}) == "MISSING_REGRESSION_TEST"
    assert classify_row({**base, "answered": False, "regression_added": False}) == "WRONG_ANSWER"
    assert classify_row({**base, "answered": True, "regression_added": False}) is None
    assert classify_row({"status": "CRASHED", "passes": False}) == "CRASHED"
    assert (
        classify_row(
            {**base, "status": "FAILED", "passes": False, "stop_reason": "cost limit $0.50 reached"}
        )
        == "BUDGET_COST"
    )
    rows = [base, {**base, "passes": False}, {**base, "passes": False, "failure_code": "WRONG_FIX"}]
    assert summarize(rows)["failures_by_code"] == {"WRONG_FIX": 2}
    assert by_code([None, "A", "A", "B"]) == {"A": 2, "B": 1}


def test_fmea_ranks_by_risk_priority():
    table = fmea({"WRONG_FIX": 2, "BUDGET_ITERATIONS": 5, "CANCELED": 9}, total_runs=20)
    codes = [r["code"] for r in table]
    # a silent wrong fix (S8, D8) outranks a frequent but obvious budget stop and cancels
    assert codes[0] == "WRONG_FIX" and codes[-1] == "CANCELED"
    top = table[0]
    assert top["rpn"] == top["severity"] * top["occurrence"] * top["detection"]
    assert top["occurrence"] == 2 and table[1]["occurrence"] == 5  # 10% → 2, 25% → 5
    assert fmea({}, 0) == []


def _ev(t, **d):
    return {"type": t, "ts": datetime.now(UTC).isoformat(), "data": d}


def test_rca_builds_the_chain_before_each_stop():
    events = [
        _ev("tool.completed", tool="read_file", ok=True),
        _ev("tool.completed", tool="run_tests", ok=False, error="2 failed"),
        _ev("guard.fired", guard="unvalidated_change"),
        _ev("loop.detected", tool="run_tests", reason="same failure 3 times"),
        _ev("run.failed", stop_reason="NO_PROGRESS: same failure 3 times"),
        _ev("tool.completed", tool="write_file", ok=True),
        _ev("verify.verdict", verdict="pass"),
        _ev("run.completed", stop_reason=None),
    ]
    runs = rca(events)
    assert len(runs) == 2
    first, second = runs
    assert first["code"] == "NO_PROGRESS" and first["first_sign"] == "run_tests failed: 2 failed"
    assert first["signals"] == [
        "run_tests failed: 2 failed",
        "guard unvalidated_change",
        "loop on run_tests: same failure 3 times",
    ]
    assert first["remedies"] and "trendlab/agent/loop_detector.py" in first["suspects"]
    assert second["code"] is None and second["signals"] == []  # clean run, chain reset


def test_failures_and_rca_commands(_trendlab_home: Path, project: Path):
    store = SessionStore(trendlab_home() / "sessions.db")
    sess = store.create_session(str(project), "scripted:m")
    sid = sess["id"] if isinstance(sess, dict) else store.latest_session(str(project))["id"]
    now = datetime.now(UTC).isoformat()
    store.append_event(sid, "tool.completed", {"tool": "run_tests", "ok": False, "error": "E"}, now)
    store.append_event(sid, "run.failed", {"stop_reason": "cost limit $0.10 reached"}, now)
    store.append_event(sid, "run.failed", {"stop_reason": "x", "failure_code": "WRONG_FIX"}, now)
    store.append_event(sid, "run.completed", {"changed_files": []}, now)
    store.close()
    runner = CliRunner()
    out = runner.invoke(app, ["failures", "--output", "json"])
    assert out.exit_code == 0, out.output
    data = json.loads(out.output)
    assert data["runs"] == 3
    assert {r["code"] for r in data["failures"]} == {"BUDGET_COST", "WRONG_FIX"}
    assert data["failures"][0]["code"] == "WRONG_FIX"  # highest risk first
    text = runner.invoke(app, ["failures"])
    assert "BUDGET_COST" in text.output and "RPN" in text.output
    r = runner.invoke(app, ["rca", sid, "--output", "json"])
    runs = json.loads(r.output)
    assert [x["code"] for x in runs] == ["BUDGET_COST", "WRONG_FIX", None]
    assert runs[0]["first_sign"] == "run_tests failed: E"
    shown = runner.invoke(app, ["rca", sid])
    assert "BUDGET_COST" in shown.output and "remedies" in shown.output
