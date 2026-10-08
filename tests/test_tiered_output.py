"""Tiered tool output (cheap-model spec §2–§3.1): parsers, budgets, traces, inspect_output,
screener hand-off and baselines."""

from pathlib import Path

from trendlab.config.schema import PermissionMode
from trendlab.permissions.engine import PermissionEngine
from trendlab.providers.base import ToolCall
from trendlab.telemetry.events import EventType
from trendlab.tools.base import ToolContext, ToolResult
from trendlab.tools.registry import default_registry
from trendlab.tools.runtime import ToolRuntime
from trendlab.tools.views import ToolOutput, tier_output
from trendlab.tools.views.screener import build_messages, parse_answer
from trendlab.tools.views.tiering import Baselines, tier_result

PYTEST_FAIL = """============================= test session starts ==============================
collected 120 items
tests/test_a.py ....F...                                                [ 50%]
=================================== FAILURES ===================================
______________________________ test_timeout_value ______________________________
tests/test_a.py:12: in test_timeout_value
    assert app.TIMEOUT == 60
E   assert 30 == 60
=========================== short test summary info ============================
FAILED tests/test_a.py::test_timeout_value - assert 30 == 60
========================= 1 failed, 119 passed in 2.31s ========================
"""
PYTEST_OK = "...\n========================= 304 passed in 41.02s =========================\n"
JEST = """ FAIL  src/sum.test.js
  ● sum › adds 1 + 2 to equal 3
    Expected: 3
    Received: 4
Tests:       1 failed, 7 passed, 8 total
Time:        1.42 s
"""
CARGO = """running 3 tests
test parse::ok ... ok
test parse::bad_input ... FAILED
test render ... ok
thread 'parse::bad_input' panicked at src/parse.rs:40:9:
assertion failed: x > 0
test result: FAILED. 2 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s
"""
GO = """--- FAIL: TestSum (0.00s)
    sum_test.go:12: got 4 want 3
FAIL
FAIL\texample.com/m\t0.004s
ok  \texample.com/m/util\t0.002s
"""
RUFF = "\n".join(
    f"app/mod{i % 3}.py:{i}:1: E501 Line too long ({110 + i} > 100)" for i in range(30)
)
RUFF += "\nFound 30 errors.\n"
ESLINT = """/src/a.ts
  12:5  error  Unexpected any  @typescript-eslint/no-explicit-any
  40:1  warning  Missing return type  @typescript-eslint/explicit-module-boundary-types
"""
TSC = "src/a.ts(12,5): error TS2322: Type 'string' is not assignable to type 'number'.\n"
DIFF = """diff --git a/app/x.py b/app/x.py
index 1..2 100644
--- a/app/x.py
+++ b/app/x.py
@@ -10,3 +10,4 @@ def f():
-    return 1
+    return 2
+    # more
diff --git a/app/y.py b/app/y.py
--- a/app/y.py
+++ b/app/y.py
@@ -1,2 +1,2 @@
-a
+b
"""


def test_pytest_failures_become_facts_and_assertions():
    out = tier_output(PYTEST_FAIL, {"exit_code": 1})
    assert out.parser == "tests_py" and out.kind == "tests"
    assert "1 failed, 119 passed" in out.tier1 and "exit 1" in out.tier1
    assert "first failing: tests/test_a.py::test_timeout_value" in out.tier1
    assert "assert 30 == 60" in out.tier2 and "tests/test_a.py:12" in out.tier2
    assert out.stats["failed"] == 1 and out.stats["ok"] is False


def test_pytest_pass_is_one_line():
    out = tier_output(PYTEST_OK, {"exit_code": 0})
    assert out.tier1.startswith("tests: 304 passed, 41.0s") and out.tier2 is None
    assert out.stats["ok"] is True


def test_js_rust_go_parsers():
    j = tier_output(JEST, {})
    assert j.parser == "tests_js" and "1 failed" in j.tier1 and "sum › adds" in j.tier1
    assert "Received: 4" in j.tier2
    c = tier_output(CARGO, {})
    assert c.parser == "tests_rs_go" and "2 passed, 1 failed" in c.tier1
    assert "parse::bad_input" in c.tier1 and "src/parse.rs:40:9" in c.tier2
    g = tier_output(GO, {})
    assert g.parser == "tests_rs_go" and "1 failing tests" in g.tier1
    assert "sum_test.go:12 got 4 want 3" in g.tier2


def test_lint_parsers_group_by_file_and_code():
    r = tier_output(RUFF, {"exit_code": 1})
    assert r.parser == "lint" and "30 findings in 3 files" in r.tier1 and "E501 ×30" in r.tier1
    assert r.tier2.startswith("app/mod0.py (10)")
    e = tier_output(ESLINT, {})
    assert e.parser == "lint" and "2 findings in 1 files" in e.tier1
    assert "@typescript-eslint/no-explicit-any" in e.tier1
    t = tier_output(TSC, {})
    assert t.parser == "lint" and "TS2322" in t.tier1
    clean = tier_output("All checks passed!\n", {"hint": "lint"})
    assert clean.tier1.startswith("lint: clean")


def test_listing_search_git_http_parsers():
    big = "\n".join(f"file_{i}.pdf" for i in range(4000)) + "\nnotes.txt\nsrc/"
    lst = tier_output(big, {"hint": "listing"})
    assert lst.parser == "listing" and "4002 entries" in lst.tier1 and "pdf ×4000" in lst.tier1
    assert len(lst.render(600)) < 700
    hits = "\n".join(f"src/m{i % 4}.py:{i}: def handler_{i}():" for i in range(60))
    s = tier_output(hits, {"hint": "search"})
    assert s.parser == "search" and "60 matches in 4 files" in s.tier1
    assert "inspect_output" in s.tier2
    d = tier_output(DIFF, {"command": "git diff"})
    assert d.parser == "gitview" and d.tier1 == "diff: 2 files, +3 -2"
    assert "app/x.py +2 -1 @10 def f():" in d.tier2
    page = "# Title\n\nIntro paragraph.\n\n## Install\n\nRun pip install foo to install.\n\n" * 3
    h = tier_output(
        page, {"hint": "http", "url": "https://x.test", "status": 200, "question": "how to install"}
    )
    assert h.parser == "http" and "HTTP 200" in h.tier1 and "pip install foo" in h.tier2


def test_generic_summary_keeps_head_tail_and_errors():
    text = "\n".join(f"line {i}" for i in range(200)) + "\nError: boom\n"
    out = tier_output(text, {"exit_code": 2, "duration_ms": 1500})
    assert out.parser == "generic" and "201 lines" in out.tier1 and "exit 2" in out.tier1
    assert "1 error-like lines; first: Error: boom" in out.tier1
    assert "line 0" in out.tier2 and "Error: boom" in out.tier2 and "omitted" in out.tier2


def test_render_respects_budget_and_points_to_tier3():
    out = ToolOutput(tier1="facts", tier2="x" * 5000, tier3_ref="t", stats={"call_id": "c1"})
    r = out.render(800)
    assert len(r) < 900 and "more via inspect_output" in r and 'inspect_output(call_id="c1")' in r
    assert ToolOutput(tier1="only").render(100) == "only"


def _runtime(project: Path, mgr, events, budgets=None) -> ToolRuntime:
    rt = ToolRuntime(
        default_registry(),
        PermissionEngine(PermissionMode.UNSAFE),
        mgr,
        events,
        ToolContext(project_root=project, session_id=mgr.session_id),
    )
    rt.tool_budgets = budgets if budgets is not None else {"shell": 300, "run_tests": 300}
    rt.baselines = Baselines(project)
    return rt


async def test_big_shell_output_is_tiered_and_inspectable(
    project: Path, manager_factory, events, recorder
):
    rt = _runtime(project, manager_factory(), events)
    cmd = "for i in $(seq 1 400); do echo row $i value; done; echo 'Error: boom'"
    res = await rt.execute(ToolCall(id="call-7", name="shell", arguments={"command": cmd}))
    assert res.ok and len(res.output) < 1500 and "401 lines" in res.output
    assert 'inspect_output(call_id="call-7")' in res.output
    assert (project / ".trendlab/traces/call-7.log").read_text().count("\n") >= 400
    ev = recorder.of_type(EventType.TOOL_OUTPUT_TIERED)[-1]
    assert ev.data["parser"] == "generic" and ev.data["raw_chars"] > ev.data["shown_chars"]
    q = await rt.execute(
        ToolCall(
            id="c8", name="inspect_output", arguments={"call_id": "call-7", "query": "row 25[0-9]"}
        )
    )
    assert q.ok and "10 of 401 lines match" in q.output and "row 250 value" in q.output
    rng = await rt.execute(
        ToolCall(
            id="c9", name="inspect_output", arguments={"call_id": "call-7", "lines": "398-401"}
        )
    )
    assert rng.ok and rng.output.startswith("lines 398-401 of 401") and "Error: boom" in rng.output
    missing = await rt.execute(
        ToolCall(id="c10", name="inspect_output", arguments={"call_id": "nope"})
    )
    assert not missing.ok and "no stored output" in missing.output
    bad = await rt.execute(ToolCall(id="c11", name="inspect_output", arguments={"call_id": "../x"}))
    assert not bad.ok


async def test_small_output_untouched_and_read_file_never_tiered(
    project: Path, manager_factory, events, recorder
):
    rt = _runtime(project, manager_factory(), events, budgets={"shell": 300, "read_file": 10})
    res = await rt.execute(ToolCall(id="s1", name="shell", arguments={"command": "echo hi"}))
    assert res.output.strip() == "hi" and "tiers" not in res.data
    (project / "big.py").write_text("\n".join(f"x{i} = {i}" for i in range(300)))
    rf = await rt.execute(ToolCall(id="r1", name="read_file", arguments={"path": "big.py"}))
    assert rf.ok and "x299 = 299" in rf.output and "tiers" not in rf.data
    assert not recorder.of_type(EventType.TOOL_OUTPUT_TIERED)


async def test_screener_used_only_for_generic_oversize_and_failure_is_safe(project: Path):
    calls = []

    async def good(text, meta):
        calls.append(meta["tool"])
        return ToolOutput(tier1="screened facts", tier2="detail")

    async def broken(text, meta):
        raise RuntimeError("provider down")

    raw = "\n".join(f"noise {i}" for i in range(2000))
    res = ToolResult(ok=True, output=raw, data={"exit_code": 0})
    out = await tier_result(
        tool="shell",
        args={"command": "x"},
        result=res,
        call_id="k1",
        project_root=project,
        budgets={"shell": 300},
        screener=good,
        screener_threshold_tokens=1000,
        baselines=None,
    )
    assert (
        out.parser == "screener" and res.output.startswith("screened facts") and calls == ["shell"]
    )
    res2 = ToolResult(ok=True, output=raw, data={"exit_code": 0})
    out2 = await tier_result(
        tool="shell",
        args={"command": "x"},
        result=res2,
        call_id="k2",
        project_root=project,
        budgets={"shell": 300},
        screener=broken,
        screener_threshold_tokens=1000,
        baselines=None,
    )
    assert out2.parser == "generic" and "2000 lines" in res2.output
    # below the threshold the screener is not consulted; parsed kinds never are
    calls.clear()
    res3 = ToolResult(ok=True, output=raw, data={"exit_code": 0})
    await tier_result(
        tool="shell",
        args={"command": "x"},
        result=res3,
        call_id="k3",
        project_root=project,
        budgets={"shell": 300},
        screener=good,
        screener_threshold_tokens=100_000,
        baselines=None,
    )
    res4 = ToolResult(ok=False, output=PYTEST_FAIL * 3, data={"exit_code": 1})
    await tier_result(
        tool="run_tests",
        args={},
        result=res4,
        call_id="k4",
        project_root=project,
        budgets={"run_tests": 50},
        screener=good,
        screener_threshold_tokens=1,
        baselines=None,
    )
    assert calls == []


def test_screener_prompt_is_small_and_answer_is_strict():
    msgs = build_messages("x" * 50_000, {"tool": "shell", "command": "make"})
    assert len(msgs) == 1 and len(msgs[0]["content"]) < 26_000 and "[elided]" in msgs[0]["content"]
    assert parse_answer("not json") is None and parse_answer('{"tier1": ""}') is None
    out = parse_answer('Sure! {"tier1": "a\\nb\\nc\\nd\\ne\\nf", "tier2": ""}')
    assert out.tier1 == "a\nb\nc\nd" and out.tier2 is None


async def test_baseline_flags_shrunken_test_run(project: Path):
    b = Baselines(project)
    first = tier_output(PYTEST_OK, {})
    assert b.check(first) is None
    shrunk = tier_output("== 100 passed in 1.0s ==\n", {})
    note = Baselines(project).check(shrunk)
    assert note and "100 tests ran, last time 304" in note
    res = ToolResult(ok=True, output=PYTEST_OK * 40, data={"exit_code": 0})
    out = await tier_result(
        tool="run_tests",
        args={},
        result=res,
        call_id="b1",
        project_root=project,
        budgets={"run_tests": 50},
        screener=None,
        screener_threshold_tokens=1,
        baselines=Baselines(project),
    )
    assert out.stats.get("anomaly") is None or "⚠" in res.output


def test_repository_map_folds_crowded_dirs_and_keeps_shallow_sources(tmp_path: Path):
    from trendlab.context.ignore import IgnoreRules
    from trendlab.context.repository_map import RepositoryMap

    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    for i in range(300):
        (root / "data" / f"s{i:03d}.csv").write_text("x")
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "test_calc.py").write_text("from calc import add\n")
    rm = RepositoryMap(root, IgnoreRules([]), max_files=100).build(use_cache=False)
    text = rm.render()
    assert "test_calc.py" in text and "calc.py" in text  # shallow sources survive truncation
    assert text.count("data/s") <= 4 and "more files (.csv ×" in text
    assert len(text) < 600
