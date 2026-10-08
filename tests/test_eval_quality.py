"""Attempt 2, part B: leakage guard, suite version, bias probes, blind labelling with kappa."""

import json
from pathlib import Path

from typer.testing import CliRunner

from trendlab.agent.judge import bias_probe, pad_diff, restyle_diff
from trendlab.benchmarks import suite as suite_mod
from trendlab.cli import app

DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"


def test_leakage_guard_and_suite_version():
    task = suite_mod.get_task("py01-off_by_one")
    assert suite_mod.leakage(task, "the model only saw the visible tests") == []
    line = next(ln.strip() for ln in task.defect.hidden_test.splitlines() if "assert" in ln)
    assert "hidden test assertion" in suite_mod.leakage(task, f"... {line} ...")
    assert "hidden test file name" in suite_mod.leakage(task, task.defect.hidden_test_file)
    # the agent writing the same obvious assertion itself is not leakage
    assert suite_mod.leakage(task, f"... {line} ...", authored=f"+    {line}\n") == []
    # question tasks have no hidden test, so nothing can leak
    question = suite_mod.get_task("rq01-where_tax")
    assert suite_mod.leakage(question, "any context at all") == []
    assert len(suite_mod.SUITE_VERSION) == 12


async def test_bias_probe_detects_presentation_sensitivity():
    assert pad_diff(DIFF).count("# NOTE") == 1 and restyle_diff(DIFF).startswith("# Summary")

    async def unbiased(messages):
        return '{"verdict": "pass", "findings": []}', "m"

    r = await bias_probe(unbiased, task="t", diff=DIFF, validation=None)
    assert r == {
        "plain": "pass",
        "padded": "pass",
        "restyled": "pass",
        "verbosity_bias": False,
        "style_bias": False,
    }

    async def swayed(messages):
        body = messages[0]["content"]
        return (
            '{"verdict": "pass"}'
            if "production-grade" in body or "NOTE:" in body
            else '{"verdict": "fix", "findings": [{"issue": "x"}]}'
        ), "m"

    r2 = await bias_probe(swayed, task="t", diff=DIFF, validation=None)
    assert r2["verbosity_bias"] and r2["style_bias"]


def test_blind_label_command_reports_kappa(tmp_path: Path):
    rows = [
        {
            "task": "t1",
            "config": "A",
            "run": 1,
            "prompt": "fix t1",
            "diff": DIFF,
            "passes": True,
            "verification": "pass",
        },
        {
            "task": "t2",
            "config": "A",
            "run": 1,
            "prompt": "fix t2",
            "diff": DIFF,
            "passes": False,
            "verification": "fail",
        },
        {
            "task": "t3",
            "config": "B",
            "run": 1,
            "prompt": "fix t3",
            "diff": DIFF,
            "passes": True,
            "verification": "pass",
        },
    ]
    rf = tmp_path / "rows.json"
    rf.write_text(json.dumps({"rows": [rows]}))
    out = tmp_path / "labels.json"
    runner = CliRunner()
    # blind order is a fixed shuffle; answer by reading the shown prompt
    res = runner.invoke(app, ["label", str(rf), "--out", str(out)], input="y\nn\ny\n")
    assert res.exit_code == 0, res.output
    labels = json.loads(out.read_text())
    assert len(labels) == 3 and "3 labelled" in res.output and "kappa" in res.output
    shown = res.output.split("labelled")[0]
    # blind: the grader never sees the config, the verifier verdict or the hidden-test result
    assert "verification" not in shown and "passes" not in shown and "fail" not in shown
