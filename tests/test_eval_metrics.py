"""Attempt 2, part A: classifier metrics, calibration, kappa, capability map, unsupported claims,
reference answers, scorecard."""

from pathlib import Path

from trendlab.agent.claims import unsupported_claims
from trendlab.agent.judge import verifier_scores
from trendlab.agent.verifier import parse_verdict
from trendlab.benchmarks import suite as suite_mod
from trendlab.benchmarks.runner import compare_rows, scorecard, summarize
from trendlab.benchmarks.stats import calibration, capability_map, classifier_metrics, cohen_kappa
from trendlab.config.schema import PermissionMode
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.telemetry.events import EventType

from .test_agent_runtime import make_agent


def test_classifier_metrics_hand_computed():
    # 3 TP, 1 FP, 2 FN, 4 TN
    pairs = [(True, True)] * 3 + [(True, False)] + [(False, True)] * 2 + [(False, False)] * 4
    m = classifier_metrics(pairs)
    assert (m["tp"], m["fp"], m["fn"], m["tn"]) == (3, 1, 2, 4)
    assert m["precision"] == 0.75 and m["recall"] == 0.6 and m["f1"] == 0.667
    assert (
        m["false_positive_rate"] == 0.2 and m["false_negative_rate"] == 0.4 and m["accuracy"] == 0.7
    )
    empty = classifier_metrics([])
    assert empty["precision"] is None and empty["f1"] is None


def test_calibration_brier_and_ece():
    perfect = calibration([(1.0, True), (0.0, False)])
    assert perfect["brier"] == 0.0 and perfect["ece"] == 0.0
    over = calibration([(0.9, False)] * 4 + [(0.9, True)] * 6)  # says 90 %, right 60 %
    assert over["ece"] == 0.3 and over["brier"] == round((4 * 0.81 + 6 * 0.01) / 10, 4)
    assert calibration([])["brier"] is None


def test_cohen_kappa():
    assert cohen_kappa(["a", "b", "a", "b"], ["a", "b", "a", "b"]) == 1.0
    assert cohen_kappa(["a", "a", "b", "b"], ["a", "b", "a", "b"]) == 0.0
    assert cohen_kappa([], []) is None
    assert cohen_kappa(["p"] * 5, ["p"] * 5) == 1.0


def test_capability_map_shows_the_jagged_edge():
    rows = [
        {"task": "1", "defect_kind": "off_by_one", "passes": True},
        {"task": "2", "defect_kind": "off_by_one", "passes": True},
        {"task": "3", "defect_kind": "shared_state", "passes": False},
        {"task": "4", "skipped": "go"},
    ]
    cm = capability_map(rows)
    assert cm["off_by_one"]["passes"] == 1.0 and cm["shared_state"]["passes"] == 0.0
    assert cm["shared_state"]["ci95"][1] < 1.0 and "?" not in cm


def test_unsupported_claims():
    ev_fail = {"changed_files": ["src/a.py"], "validation_runs": [{"ok": True}, {"ok": False}]}
    assert unsupported_claims("Fixed. All tests pass.", ev_fail) == [
        "claims tests pass but the last validation failed"
    ]
    assert unsupported_claims(
        "The tests pass now.", {"changed_files": ["a.py"], "validation_runs": []}
    ) == ["claims tests pass but no validation ran"]
    ok = {"changed_files": ["src/a.py", "tests/test_a.py"], "validation_runs": [{"ok": True}]}
    assert unsupported_claims("Fixed src/a.py and added a regression test; 12 passed.", ok) == []
    assert "claims b.py was changed but it was not" in unsupported_claims(
        "I also updated b.py.", ok
    )
    no_test = {"changed_files": ["src/a.py"], "validation_runs": [{"ok": True}]}
    assert unsupported_claims("I added a regression test.", no_test) == [
        "claims a test was added but no test file changed"
    ]
    assert (
        unsupported_claims(
            "The README describes a demo.", {"changed_files": [], "validation_runs": []}
        )
        == []
    )


async def test_run_result_carries_unsupported_claims(
    project: Path, manager_factory, events, recorder
):
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="w",
                        name="write_file",
                        arguments={"path": "src/x.py", "content": "x = 2\n"},
                    )
                ]
            ),
            ModelResponse(text="Done — all tests pass."),
        ]
    )
    agent, _ = make_agent(
        project, manager_factory(), events, provider, mode=PermissionMode.UNSAFE, validation={}
    )
    result = await agent.run("set x to 2")
    assert result.unsupported_claims == ["claims tests pass but no validation ran"]
    assert (
        recorder.of_type(EventType.CLAIM_UNSUPPORTED)[0].data["claims"] == result.unsupported_claims
    )


def test_verifier_confidence_and_scores():
    v = parse_verdict('{"verdict": "pass", "confidence": 0.8, "findings": []}')
    assert v.confidence == 0.8 and v.to_json()["confidence"] == 0.8
    assert parse_verdict('{"verdict": "pass", "confidence": 7}').confidence is None
    rows = [
        {"verification": "pass", "passes": True, "verifier_confidence": 0.9},
        {"verification": "pass", "passes": False, "verifier_confidence": 0.9},
        {"verification": "fail", "passes": False, "verifier_confidence": 0.1},
        {"verification": "fix", "passes": True, "verifier_confidence": 0.4},
    ]
    s = verifier_scores(rows)
    assert s["classifier"]["tp"] == 1 and s["classifier"]["fp"] == 1 and s["classifier"]["fn"] == 1
    assert s["calibration"]["n"] == 4 and s["calibration"]["brier"] is not None


def test_reference_answers_and_scorecard():
    task = suite_mod.get_task("py01-off_by_one")
    ref = suite_mod.reference_content(task, task.answer_file)
    assert ref is not None and task.defect.old in ref and task.defect.new not in ref
    a = [
        {
            "task": "t1",
            "passes": True,
            "status": "COMPLETED",
            "cost": 0.01,
            "defect_kind": "k1",
            "lang": "python",
            "tier": "base",
            "exact_match": True,
            "ref_similarity": 1.0,
            "unsupported_claims": [],
        },
        {
            "task": "t2",
            "passes": False,
            "status": "COMPLETED",
            "cost": 0.02,
            "defect_kind": "k2",
            "lang": "python",
            "tier": "hard",
            "exact_match": False,
            "ref_similarity": 0.8,
            "unsupported_claims": ["x"],
        },
    ]
    s = summarize(a)
    assert s["exact_match"] == 0.5 and s["ref_similarity"] == 0.9 and s["hallucination_rate"] == 0.5
    assert (
        s["capability"]["by_defect"]["k2"]["passes"] == 0.0
        and s["capability"]["by_tier"]["base"]["n"] == 1
    )
    md = scorecard([{"config": "A", **s}, {"config": "B", **s}], compare_rows(a, a))
    assert md.startswith("| metric | A | B |") and "hallucination rate" in md
    assert "Weak spots (A): k2" in md and "exact sign test p = 1.0" in md
