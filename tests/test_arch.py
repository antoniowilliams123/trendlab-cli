"""Uplift U24: import graph, cycles, layer rules, new violations, parsimony."""

from pathlib import Path

from trendlab.agent.arch import cycles, graph, new_violations, report
from trendlab.benchmarks.runner import parsimony
from trendlab.benchmarks.suite import get_task


def _proj(tmp: Path) -> Path:
    root = tmp / "p"
    files = {
        "app/__init__.py": "",
        "app/core.py": "from app.util import helper\n\ndef run():\n    return helper()\n",
        "app/util.py": "def helper():\n    return 1\n",
        "app/ui.py": "import app.core\n\n\ndef show():\n    from app.db import save  # lazy: not an edge\n",
        "app/db.py": "def save():\n    pass\n",
        "tests/test_core.py": "from app.core import run\n",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def test_graph_counts_module_level_project_imports_only(tmp_path: Path):
    g = graph(_proj(tmp_path))
    assert g["app.core"] == {"app.util"} and g["app.ui"] == {"app.core"}
    assert "tests.test_core" not in g and cycles(g) == []


def test_report_rules_and_cycles(tmp_path: Path):
    root = _proj(tmp_path)
    (root / "app/util.py").write_text("from app.core import run\n\ndef helper():\n    return 1\n")
    r = report(root, ["app.core -> app.util"])
    assert r["cycles"] == [["app.core", "app.util"]]
    assert r["violations"] == [
        "app.core imports app.util (rule: app.core -> app.util is forbidden)"
    ]


def test_new_violations_judge_only_what_the_change_added(tmp_path: Path):
    root = _proj(tmp_path)
    old = (root / "app/util.py").read_text()
    new = "from app.ui import show\n\ndef helper():\n    return 1\n"
    (root / "app/util.py").write_text(new)
    probs = new_violations(
        root, {"app/util.py": new}, ["app.util -> app.ui"], before_sources={"app/util.py": old}
    )
    assert "new import app.util -> app.ui breaks rule app.util -> app.ui" in probs
    assert any(p.startswith("new import cycle") for p in probs)
    # an existing cycle is not blamed on an unrelated change
    clean = new_violations(
        root,
        {"app/db.py": "def save():\n    return 2\n"},
        [],
        before_sources={"app/db.py": "def save():\n    pass\n"},
    )
    assert clean == []


def test_parsimony_counts_source_lines_beyond_the_minimal_fix():
    task = get_task("py01-off_by_one")
    minimal = (
        "--- a/shop/util.py\n+++ b/shop/util.py\n"
        "-    return [items[i : i + size] for i in range(0, len(items) - 1, size)]\n"
        "+    return [items[i : i + size] for i in range(0, len(items), size)]\n"
        "--- a/tests/test_util.py\n+++ b/tests/test_util.py\n+def test_x(): pass\n"
    )
    assert parsimony(task, minimal) == {
        "source_lines_changed": 2,
        "minimal_lines": 2,
        "excess_lines": 0,
    }
    bloated = minimal + "--- a/shop/util.py\n+++ b/shop/util.py\n" + "+# extra\n" * 6
    assert parsimony(task, bloated)["excess_lines"] == 6
    assert parsimony(get_task("rq01-where_tax"), minimal) == {}  # questions have no fix size
