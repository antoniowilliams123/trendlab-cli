"""Placeholder test scripts are not tests; run_tests tests the project around the edits."""

import json
from pathlib import Path

from trendlab.context.validation import (
    detect_validation_commands,
    is_placeholder_script,
    nearest_project,
)
from trendlab.tools.base import ToolContext
from trendlab.tools.tests import RunTestsInput, RunTestsTool

NPM_INIT = 'echo "Error: no test specified" && exit 1'


def _home(tmp: Path) -> Path:
    """A home-like root: npm-init placeholder at the top, a real Python project below."""
    home = tmp / "home"
    (home / "data" / "scripts" / "tests").mkdir(parents=True)
    (home / "package.json").write_text(json.dumps({"scripts": {"test": NPM_INIT}}))
    scripts = home / "data" / "scripts"
    (scripts / "tool.py").write_text("def add(a, b):\n    return a + b\n")
    (scripts / "tests" / "test_tool.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parents[1]))\n"
        "from tool import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    (home / "web").mkdir()
    (home / "web" / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    return home


def test_placeholders_are_not_test_commands(tmp_path: Path):
    assert is_placeholder_script(NPM_INIT) and is_placeholder_script("echo todo")
    assert not is_placeholder_script("jest --ci") and not is_placeholder_script("pytest -q")
    home = _home(tmp_path)
    assert "test" not in detect_validation_commands(home)
    assert detect_validation_commands(home / "web")["test"] == "npm test"


def test_nearest_project_follows_the_edits(tmp_path: Path):
    home = _home(tmp_path)
    scripts = home / "data" / "scripts"
    assert nearest_project(home, ["data/scripts/tool.py"]) == scripts.resolve()
    assert nearest_project(home, ["data/scripts/tests/test_tool.py"]) == scripts.resolve()
    assert nearest_project(home, ["data/scripts/tool.py", "web/app.js"]) == home.resolve()
    assert nearest_project(home, ["notes.txt"]) == home.resolve()
    assert nearest_project(home, []) == home.resolve()


async def test_run_tests_runs_in_the_edited_project(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TRENDLAB_SANDBOX", "off")
    home = _home(tmp_path)
    ctx = ToolContext(project_root=home, session_id="t", validation_commands={})
    tool = RunTestsTool()
    res = await tool.run(RunTestsInput(), ctx)  # nothing edited, root has no tests
    assert not res.ok and "pass path=" in res.output
    ctx.changed_files = {"data/scripts/tool.py": [""]}
    res = await tool.run(RunTestsInput(), ctx)
    assert res.ok, res.output
    assert res.output.startswith("(ran in data/scripts/)") and "1 passed" in res.output
    assert res.data["cwd"] == "data/scripts"
    ctx.changed_files = {}
    res = await tool.run(RunTestsInput(path="data/scripts"), ctx)
    assert res.ok and res.data["cwd"] == "data/scripts"


def test_validation_counts_the_edited_project(tmp_path: Path):
    from types import SimpleNamespace

    from trendlab.agent.runtime import AgentRuntime

    home = _home(tmp_path)
    ctx = ToolContext(project_root=home, session_id="t", validation_commands={})
    fake = SimpleNamespace(tools=SimpleNamespace(ctx=ctx))
    available = AgentRuntime.validation_available.fget
    assert available(fake) is False
    ctx.changed_files = {"data/scripts/tool.py": [""]}
    assert available(fake) is True
