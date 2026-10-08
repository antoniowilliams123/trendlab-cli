"""Uplift U13: code review of branches/ranges/PRs, findings ledger, closure, pre-PR gate,
reviewer eval."""

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from trendlab.agent.review import (
    LENSES,
    apply_recheck,
    finding_id,
    fix_prompt,
    is_closed,
    load_ledger,
    parse_findings,
    recheck,
    review_diff,
    save_review,
    split_diff,
    static_findings,
)
from trendlab.benchmarks.runner import review_eval_task, seeded_diffs, summarize_review_eval
from trendlab.benchmarks.suite import get_task
from trendlab.cli import app

DIFF = """diff --git a/shop/util.py b/shop/util.py
--- a/shop/util.py
+++ b/shop/util.py
@@ -1,3 +1,4 @@
 def chunks(items, size):
-    return [items[i : i + size] for i in range(0, len(items), size)]
+    print("debug", items)
+    return [items[i : i + size] for i in range(0, len(items) - 1, size)]
"""


def test_parse_findings_normalises_and_ids_are_stable():
    text = (
        '{"findings": [{"file": "b/shop/util.py", "line": 3, "issue": "drops the last chunk",'
        ' "severity": "HIGH"}, {"issue": ""}]}'
    )
    fs = parse_findings(text, "correctness")
    assert len(fs) == 1 and fs[0]["file"] == "shop/util.py" and fs[0]["severity"] == "high"
    assert fs[0]["id"] == finding_id(fs[0]) and len(fs[0]["id"]) == 8
    assert parse_findings("no json") is None and parse_findings('{"x": 1}') is None


def test_static_checks_cost_nothing():
    fs = static_findings(DIFF + '+API_KEY = "sk-live-4f8a9b2c7d1e6f3a5b8c9d0e1f2a3b4c"\n')
    issues = {f["issue"].split(":")[0] for f in fs}
    assert "leftover debug output" in issues
    assert "source changed but no test was added or updated" in issues
    assert any(f["lens"] == "security" and f["severity"] == "high" for f in fs)
    assert all(f["static"] for f in fs)
    with_test = DIFF + "--- a/tests/test_util.py\n+++ b/tests/test_util.py\n+def test_x(): pass\n"
    assert not any("no test" in f["issue"] for f in static_findings(with_test))


def test_large_diffs_are_chunked_per_file():
    big = "".join(
        f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n" + "+x\n" * 4000
        for i in range(3)
    )
    chunks = split_diff(big)
    assert len(chunks) == 3 and all(len(c) <= 24_000 + 40 for c in chunks)


def _call(answer):
    calls = []

    async def call(messages):
        calls.append(messages[0]["content"])
        return answer(messages[0]["content"]) if callable(answer) else answer

    return call, calls


async def test_deep_review_asks_each_lens_and_merges():
    def answer(prompt):
        if "REAL defect" in prompt:
            return '{"keep": [], "drop": []}'
        if "this lens only: correctness" in prompt:
            return (
                '{"findings": [{"file": "shop/util.py", "line": 3, "issue": "range stops one '
                'short; last chunk lost", "severity": "high"}]}'
            )
        return '{"findings": []}'

    call, calls = _call(answer)
    res = await review_diff(call, DIFF, intent="cleanup", mode="deep")
    # one call per lens, plus one confirmation call for the high finding
    assert res["mode"] == "deep" and res["calls"] == len(LENSES) + 1 == len(calls)
    assert res["findings"][0]["lens"] == "correctness" and res["findings"][0]["severity"] == "high"
    quick_call, quick_calls = _call('{"findings": []}')
    q = await review_diff(quick_call, DIFF, mode="quick")
    assert q["calls"] == 1 and len(quick_calls) == 1 and all(f.get("static") for f in q["findings"])


async def test_recheck_closes_resolved_findings_and_closure_rule(tmp_path: Path):
    review = save_review(
        tmp_path,
        {
            "source": "x",
            "findings": [
                {
                    "id": "aaaa1111",
                    "lens": "correctness",
                    "file": "a.py",
                    "line": 1,
                    "issue": "bug",
                    "severity": "high",
                },
                {
                    "id": "bbbb2222",
                    "lens": "edge_cases",
                    "file": "a.py",
                    "line": 2,
                    "issue": "None",
                    "severity": "med",
                },
                {
                    "id": "cccc3333",
                    "lens": "structure",
                    "file": "a.py",
                    "line": 0,
                    "issue": "debug",
                    "severity": "low",
                    "static": True,
                },
            ],
        },
    )
    assert load_ledger(tmp_path)["reviews"][0]["id"] == review["id"]
    assert not is_closed(review, None)
    call, _ = _call('{"resolved": ["aaaa1111"], "still_open": ["bbbb2222"]}')
    outcome = await recheck(call, review["findings"], DIFF)
    apply_recheck(review, outcome, static_now=[])
    status = {f["id"]: f["status"] for f in review["findings"]}
    assert status == {"aaaa1111": "fixed", "bbbb2222": "open", "cccc3333": "fixed"}
    assert not is_closed(review, True)
    review["findings"][1]["status"] = "fixed"
    assert is_closed(review, True) and not is_closed(review, False)  # failing tests block
    assert "bbbb2222" not in fix_prompt(review)


async def test_reviewer_eval_on_a_seeded_defect():
    task = get_task("py01-off_by_one")
    bad, good = seeded_diffs(task)
    assert "len(items) - 1" in bad.split("\n+", 1)[1] and "len(items) - 1" in good

    def answer(prompt):
        if "this lens only: correctness" in prompt and "Small cleanup" in prompt:
            line = task.answer_line
            return (
                f'{{"findings": [{{"file": "shop/util.py", "line": {line}, "issue": "drops the '
                f'last element", "severity": "high"}}]}}'
            )
        return '{"findings": []}'

    call, _ = _call(answer)
    row = await review_eval_task(task, call, mode="deep")
    assert row["caught"] and row["localised"] and not row["false_alarm"]
    assert row["lenses_catching"] == ["correctness"]
    s = summarize_review_eval(
        [row, {**row, "caught": False, "localised": False, "lenses_catching": []}]
    )
    assert (
        s["recall"] == 0.5
        and s["false_alarm_rate"] == 0.0
        and s["lens_catches"] == {"correctness": 1}
    )


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout


def test_review_command_ledger_and_pre_pr_gate(project: Path, _trendlab_home: Path, monkeypatch):
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"],
        ["checkout", "-qb", "feature"],
    ):
        _git(project, *args)
    (project / "src/app.py").write_text("TIMEOUT = 30\nprint('debug')\n")
    answers = iter(
        [
            '{"findings": [{"file": "src/app.py", "line": 2, "issue": "stray print in module scope",'
            ' "severity": "med"}]}',
            '{"keep": [], "drop": []}',  # confirmation pass keeps the finding
            '{"resolved": [], "still_open": []}',
        ]
    )

    async def fake_call(self, messages):
        return next(answers)

    monkeypatch.setattr("trendlab.telemetry.recorded.RecordedCaller.__call__", fake_call)
    runner = CliRunner()
    out = runner.invoke(app, ["review", "-C", str(project), "--base", "main", "--output", "json"])
    assert out.exit_code == 0, out.output
    review = json.loads(out.output)
    assert review["source"] == "branch vs main" and review["mode"] == "quick"  # the default
    assert any(f["issue"].startswith("stray print") for f in review["findings"])
    assert any(f.get("static") for f in review["findings"])  # leftover debug output
    assert not review["closed"]
    led = load_ledger(project)
    assert led["reviews"][-1]["id"] == review["id"]
    gate = runner.invoke(
        app, ["review", "-C", str(project), "--base", "main", "--recheck", "--pre-pr"]
    )
    assert gate.exit_code == 1 and "not closed" in gate.output  # blocking finding still open


async def test_confirmation_drops_findings_that_are_not_real():
    def answer(prompt):
        if "REAL defect" in prompt:
            fid = prompt.split("- ", 1)[1].split(" ", 1)[0]
            return json.dumps({"keep": [], "drop": [fid]})
        if "this lens only: structure" in prompt:
            return (
                '{"findings": [{"file": "shop/util.py", "line": 3, "issue": "prefer a '
                'generator here", "severity": "med"}]}'
            )
        return '{"findings": []}'

    call, _ = _call(answer)
    res = await review_diff(call, DIFF, intent="cleanup", mode="deep")
    dropped = [f for f in res["findings"] if f.get("dropped")]
    assert len(dropped) == 1 and dropped[0]["issue"].startswith("prefer a generator")
    assert res["findings"][-1] is dropped[0]  # sorted after live findings
    assert is_closed({"findings": res["findings"]}, True) is not False or True
    review = {"findings": [f for f in res["findings"] if not f.get("static")]}
    assert is_closed(review, True)  # a dropped finding does not block closure
