"""Uplift U14: code-health snapshots, size-adjusted trend, backfill, engine job."""

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from trendlab.cli import app
from trendlab.engine.health import backfill, health_projects, history, snapshot, trend
from trendlab.engine.inbox import Inbox

SMALL = "def add(a, b):\n    return a + b\n"
BRANCHY = (
    "def f(x):\n"
    + "".join(f"    if x == {i}:\n        return {i}\n" for i in range(12))
    + "    return -1\n"
)


def _repo(root: Path, files: dict[str, str]):
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-qm",
            "c",
            "--allow-empty",
        ],
        check=True,
    )


def test_snapshot_measures_tests_complexity_and_markers(tmp_path: Path):
    _repo(
        tmp_path / "r",
        {
            "app/a.py": SMALL + "# TODO: tidy\n",
            "app/b.py": BRANCHY,
            "tests/test_a.py": "from app.a import add\n\ndef test_add():\n"
            "    assert add(1, 2) == 3\n",
        },
    )
    s = snapshot(tmp_path / "r")
    assert s["source_files"] == 2 and s["test_files"] == 1
    assert s["functions"] == 2 and s["complex_functions"] == 1
    assert s["complex_function_share"] == 0.5 and s["debt_markers"] == 1
    assert 0 < s["test_ratio"] < 1


def test_trend_is_size_adjusted():
    base = {
        "test_ratio": 0.4,
        "long_function_share": 0.02,
        "complex_function_share": 0.08,
        "mean_complexity": 4.0,
        "duplication": 0.02,
        "debt_per_kloc": 0.1,
        "dependencies": 7,
        "source_lines": 10_000,
    }
    grown = {**base, "source_lines": 20_000}  # twice the code, same shares: not degradation
    assert trend(base, grown)["degraded"] is False and trend(base, grown)["source_growth"] == 10_000
    worse = {**base, "complex_function_share": 0.12, "test_ratio": 0.3, "dependencies": 8}
    t = trend(base, worse)
    assert set(t["worse"]) == {"complex_function_share", "test_ratio", "dependencies"}
    better = {**base, "duplication": 0.01}
    assert trend(base, better)["better"] == ["duplication"]


def test_backfill_measures_past_commits_without_touching_the_tree(tmp_path: Path):
    root = tmp_path / "r"
    _repo(root, {"app/a.py": SMALL})
    (root / "app/b.py").write_text(BRANCHY)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-qm",
            "branchy",
        ],
        check=True,
    )
    (root / "app/dirty.py").write_text("x = 1\n")  # uncommitted: must survive
    past = backfill(root, commits=2, step=1)
    assert [p["complex_functions"] for p in past] == [0, 1]  # oldest first
    assert (root / "app/dirty.py").exists()


async def test_engine_health_job_files_a_card_on_degradation(tmp_path: Path, _trendlab_home: Path):
    root = tmp_path / "r"
    _repo(root, {"app/a.py": SMALL, "tests/test_a.py": "def test_x():\n    assert 1\n"})
    inbox = Inbox(tmp_path / "inbox.db")
    first = await health_projects(inbox, [root])
    assert "trend" not in first[0]  # no baseline yet
    (root / "app/b.py").write_text(BRANCHY)  # complexity share jumps
    second = await health_projects(inbox, [root])
    cards = inbox.list(str(root))
    inbox.close()
    assert (
        second[0]["trend"]["degraded"] and "complex_function_share" in second[0]["trend"]["worse"]
    )
    assert cards and cards[0]["title"].startswith("code health degraded")
    assert len(history(root)) == 2


def test_health_command(tmp_path: Path, _trendlab_home: Path):
    root = tmp_path / "r"
    _repo(root, {"app/a.py": SMALL})
    out = CliRunner().invoke(app, ["health", "-C", str(root), "--output", "json", "--save"])
    data = json.loads(out.output)
    assert data["snapshot"]["functions"] == 1 and data["trend"] is None
    assert len(history(root)) == 1
