"""Uplift U5: diff-shape scope budget, dependency gate, generated-test strength."""

from pathlib import Path

from rich.console import Console

from trendlab.agent.scope import diff_shape, is_test_path, scope_nudge
from trendlab.app import TrendLabApp
from trendlab.config.loader import load_config
from trendlab.config.schema import GovernanceConfig, PermissionMode, RemoteApprovalConfig
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.telemetry.events import EventType
from trendlab.ui.activity import run_footer

from .test_agent_runtime import make_agent

DIFF_SRC = (
    "--- a/src/x.py\n+++ b/src/x.py\n@@ -1 +1,4 @@\n-x = 1\n+x = 2\n+def helper():\n+    return 1\n"
)
DIFF_TEST = (
    "--- /dev/null\n+++ b/tests/test_x.py\n@@ -0,0 +1,2 @@\n+def test_x():\n+    assert True\n"
)
DIFF_DEP = '--- a/pyproject.toml\n+++ b/pyproject.toml\n@@ -5,2 +5,3 @@\n dependencies = [\n+    "requests>=2",\n ]\n'


def test_diff_shape_budgets_and_dependency_gate():
    assert is_test_path("tests/test_x.py") and not is_test_path("src/x.py")
    rep = diff_shape({"src/x.py": [DIFF_SRC], "tests/test_x.py": [DIFF_TEST]}, "fix the bug in x")
    assert (
        rep.task_kind == "fix" and rep.files == 2 and rep.source_files == 1 and rep.test_files == 1
    )
    assert rep.new_files == ["tests/test_x.py"] and rep.added == 5 and rep.removed == 1
    assert rep.new_definitions == ["src/x.py:helper"] and rep.ok
    many = {f"src/m{i}.py": [DIFF_SRC] for i in range(6)}
    rep2 = diff_shape(many, "fix the crash")
    assert not rep2.ok and "6 source files" in rep2.problems[0] and "budget 4" in rep2.problems[0]
    assert "new functions/classes" in rep2.problems[1]
    assert diff_shape(many, "add a new reporting module").ok  # a change task has a wider budget
    dep = diff_shape({"pyproject.toml": [DIFF_DEP]}, "fix the parser")
    assert (
        not dep.ok and dep.new_dependencies == ["requests"] and "not requested" in dep.problems[0]
    )
    assert diff_shape({"pyproject.toml": [DIFF_DEP]}, "add requests as a dependency and use it").ok
    assert "Scope check" in scope_nudge(dep)


async def test_scope_nudge_once_then_report_on_result(
    project: Path, manager_factory, events, recorder
):
    writes = [
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id=f"w{i}",
                    name="write_file",
                    arguments={"path": f"src/m{i}.py", "content": f"v = {i}\n"},
                )
            ]
        )
        for i in range(5)
    ]
    provider = ScriptedProvider(
        writes
        + [
            ModelResponse(text="fixed it"),
            ModelResponse(text="each file was needed: they share the constant"),
        ]
    )
    agent, _ = make_agent(
        project, manager_factory(), events, provider, mode=PermissionMode.UNSAFE, validation={}
    )
    agent.scope_config = GovernanceConfig()
    result = await agent.run("fix the broken constant")
    assert result.status == "COMPLETED" and result.scope and not result.scope["ok"]
    assert result.scope["source_files"] == 5 and "5 source files" in result.scope["problems"][0]
    nudges = [
        m
        for m in agent.messages
        if m.get("role") == "user" and "Scope check" in str(m.get("content"))
    ]
    assert len(nudges) == 1 and "each file was needed" in result.text
    checks = recorder.of_type(EventType.SCOPE_CHECKED)
    assert len(checks) == 2 and checks[0].data["ok"] is False
    assert "scope !" in run_footer(result)
    assert "scope_budget" in [e.data["guard"] for e in recorder.of_type(EventType.GUARD_FIRED)]


async def test_weak_generated_test_is_detected(project: Path, _trendlab_home: Path):
    (project / "calc.py").write_text("def mul(a, b):\n    return a * b + 1\n")
    (project / "test_calc.py").write_text(
        "from calc import mul\n\n\ndef test_mul():\n    assert mul(3, 4) == 13\n"
    )
    (project / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-q -p no:cacheprovider'\n"
    )
    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    # the model 'fixes' mul and adds a test that does not depend on the fix
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="p",
                        name="patch_file",
                        arguments={"path": "calc.py", "old_text": " + 1", "new_text": ""},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="t0",
                        name="patch_file",
                        arguments={
                            "path": "test_calc.py",
                            "old_text": "== 13",
                            "new_text": "== 12",
                        },
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="w",
                        name="write_file",
                        arguments={
                            "path": "tests/test_regress.py",
                            "content": "def test_regress():\n    assert 1 + 1 == 2\n",
                        },
                    )
                ]
            ),
            ModelResponse(tool_calls=[ToolCall(id="r", name="run_tests", arguments={})]),
            ModelResponse(text="fixed mul and added a regression test"),
            ModelResponse(text="the regression test is now meaningful"),
        ]
    )
    tl = TrendLabApp(
        project,
        cfg,
        provider=provider,
        model_ref="scripted:m",
        permission_mode=PermissionMode.UNSAFE,
        console=Console(quiet=True),
    )
    await tl.start(interactive=False)
    try:
        result = await tl.run_prompt("fix the bug in mul: it returns one too many")
    finally:
        await tl.stop()
    assert result.status == "COMPLETED"
    # the scope check ran with the real test command, saw the weak test, nudged once, and the files are intact
    assert (project / "calc.py").read_text() == "def mul(a, b):\n    return a * b\n"
    assert result.scope and result.scope["test_strength"] in {"strong", "weak"}
    assert "weak" in run_footer(result) or result.scope["test_strength"] == "strong"


def test_test_gaming_is_caught_unless_the_task_asks_for_it():
    from trendlab.agent.scope import diff_shape, test_gaming

    skip = {
        "tests/test_x.py": [
            "+++ b/tests/test_x.py\n+@pytest.mark.skip(reason='flaky')\n def test_a():"
        ]
    }
    gone = {
        "tests/test_x.py": [
            "--- a/tests/test_x.py\n-    assert total == 90\n-    assert ok\n+    pass"
        ]
    }
    deleted = {"tests/test_y.py": ["--- a/tests/test_y.py\n+++ /dev/null\n-def test_b(): assert 1"]}
    ci = {
        ".github/workflows/ci.yml": ["+++ b/.github/workflows/ci.yml\n+      run: pytest || true"]
    }
    swapped = {"tests/test_x.py": ["-    assert total == 30\n+    assert total == 90"]}
    assert test_gaming(skip, "fix the total") == ["tests/test_x.py: skip/xfail added"]
    assert test_gaming(gone, "fix the total") == ["tests/test_x.py: 2 assertion(s) removed"]
    assert test_gaming(deleted, "fix it")[0].endswith("test file deleted")
    assert test_gaming(ci, "fix ci")[0].endswith("CI/test command loosened")
    assert test_gaming(swapped, "fix the total") == []  # an assertion changed, not removed
    assert test_gaming(skip, "skip the flaky test until Monday") == []  # asked for
    rep = diff_shape(gone, "fix the total")
    assert not rep.ok and rep.test_gaming and "weakened" in rep.problems[-1]
