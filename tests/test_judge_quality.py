"""Uplift U2: pairwise judge with position swap, verifier rubric, judge accuracy, second opinion."""

from pathlib import Path

from rich.console import Console

from trendlab.agent.judge import judge_accuracy, pairwise, parse_pairwise
from trendlab.agent.verifier import PROMPT, parse_verdict
from trendlab.app import TrendLabApp
from trendlab.benchmarks.runner import summarize
from trendlab.config.loader import load_config
from trendlab.config.schema import PermissionMode, RemoteApprovalConfig
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.telemetry.events import EventType

PASS = '{"verdict": "pass", "findings": []}'
FAIL = (
    '{"verdict": "fail", "findings": [{"file": "src/x.py", "line": 1, '
    '"issue": "x should stay 1", "severity": "high"}]}'
)


async def test_pairwise_swaps_order_and_flags_position_bias():
    answers = ['{"winner": "A"}', '{"winner": "B"}']  # consistent: candidate a wins both ways

    async def call(messages):
        return answers.pop(0)

    v = await pairwise(call, task="fix x", a="patch-a", b="patch-b")
    assert v == {"winner": "a", "position_bias": False, "orders": ["A", "B"], "usable": True}
    answers[:] = ['{"winner": "A"}', '{"winner": "A"}']  # always the first shown → bias
    v2 = await pairwise(call, task="fix x", a="patch-a", b="patch-b")
    assert v2["winner"] == "tie" and v2["position_bias"] is True
    answers[:] = [
        "nonsense",
        "still nonsense",
        '{"winner": "B"}',
    ]  # first order fails even after repair
    v3 = await pairwise(call, task="fix x", a="a", b="b")
    assert v3["winner"] == "tie" and v3["usable"] is False
    assert parse_pairwise('{"winner": "tie"}') == "TIE" and parse_pairwise("{}") is None


def test_verifier_rubric_parsing_and_bias_instruction():
    v = parse_verdict(
        '{"verdict": "pass", "rubric": {"correctness": 2, "minimality": 5, "safety": "x", '
        '"tests": 1}, "findings": []}'
    )
    assert v.rubric == {"correctness": 2, "minimality": 2, "tests": 1}
    assert v.to_json()["rubric"]["minimality"] == 2
    assert "Ignore length" in PROMPT and "rubric" in PROMPT


def test_judge_accuracy_against_ground_truth():
    rows = [
        {"task": "a", "passes": True, "verification": "pass", "cost": 0.0, "status": "COMPLETED"},
        {"task": "b", "passes": True, "verification": "fix", "cost": 0.0, "status": "COMPLETED"},
        {"task": "c", "passes": False, "verification": "pass", "cost": 0.0, "status": "COMPLETED"},
        {"task": "d", "passes": False, "verification": "fail", "cost": 0.0, "status": "FAILED"},
        {"task": "e", "passes": True, "verification": None, "cost": 0.0, "status": "COMPLETED"},
    ]
    j = judge_accuracy(rows)
    assert j["judged"] == 4 and j["pass_precision"] == 0.5
    assert j["false_pass_rate"] == 0.5 and j["false_flag_rate"] == 0.5
    assert summarize(rows)["judge"]["judged"] == 4
    assert judge_accuracy([]) == {"judged": 0}


async def test_second_opinion_downgrades_pass_on_disagreement(project: Path, _trendlab_home: Path):
    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    cfg.providers["second"] = cfg.providers["openai"].model_copy()
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    cfg.routing["verifier"] = "scripted:m"
    cfg.verification.second_opinion = "second:m"
    cfg.verification.min_diff_lines = 0
    cfg.verification.min_files = 1
    write = ModelResponse(
        tool_calls=[
            ToolCall(
                id="w", name="write_file", arguments={"path": "src/x.py", "content": "x = 2\n"}
            )
        ]
    )
    # lead and first verifier share the scripted provider: author, verify, author again, verify
    lead = ScriptedProvider(
        [
            write,
            ModelResponse(text="wrote x"),
            ModelResponse(text=PASS),
            ModelResponse(text="done after fix round"),
            ModelResponse(text=PASS),
        ]
    )
    second = ScriptedProvider([ModelResponse(text=FAIL), ModelResponse(text=PASS)])
    seen = []
    tl = TrendLabApp(
        project,
        cfg,
        provider=lead,
        model_ref="scripted:m",
        permission_mode=PermissionMode.UNSAFE,
        console=Console(quiet=True),
    )
    await tl.start(interactive=False)
    tl.gateway.register("second:m", second)
    tl.events.subscribe(
        lambda e: seen.append(e.data) if e.type == EventType.VERIFY_SECOND_OPINION else None
    )
    try:
        result = await tl.run_prompt("set x to 2")
    finally:
        await tl.stop()
    assert seen and seen[0]["agree"] is False and seen[0]["models"] == ["scripted:m", "second:m"]
    assert seen[1]["agree"] is True
    assert result.status == "COMPLETED" and result.verification["verdict"] == "pass"
    # the disagreement cost the author one round: the findings were handed back
    handed = [
        m
        for m in tl.agent.messages
        if m.get("role") == "user" and "x should stay 1" in str(m.get("content"))
    ]
    assert handed
