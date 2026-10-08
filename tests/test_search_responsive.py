"""search_text and glob on a huge tree: never freeze the UI, stop at a deadline, skip big and
binary files, stop when the run is cancelled."""

import asyncio
import time
from pathlib import Path

import pytest

import trendlab.tools.files as files_mod
from trendlab.context.ignore import IgnoreRules
from trendlab.tools.base import ToolContext
from trendlab.tools.files import GlobInput, GlobTool, SearchTextInput, SearchTextTool


@pytest.fixture(autouse=True)
def no_ripgrep(monkeypatch):
    monkeypatch.setattr(files_mod.shutil, "which", lambda name: None)


def _ctx(root: Path) -> ToolContext:
    return ToolContext(project_root=root, session_id="t", ignore_rules=IgnoreRules([]))


def _tree(root: Path, n: int = 40) -> None:
    for i in range(n):
        d = root / f"d{i % 4}"
        d.mkdir(exist_ok=True)
        (d / f"f{i}.py").write_text(f"x = {i}\nneedle_{i} = 1\n")


async def test_big_and_binary_files_are_skipped(tmp_path: Path):
    (tmp_path / "small.py").write_text("needle = 1\n")
    (tmp_path / "huge.txt").write_text("needle\n" * 400_000)  # > 2 MB
    (tmp_path / "data.parquet").write_text("needle\n")
    res = await SearchTextTool().run(SearchTextInput(pattern="needle"), _ctx(tmp_path))
    assert res.output.startswith("small.py:1:") and "huge.txt" not in res.output
    assert "data.parquet" not in res.output


async def test_ui_keeps_running_while_a_slow_search_works(tmp_path: Path, monkeypatch):
    _tree(tmp_path)
    real_iter = files_mod._iter_files

    def slow_iter(*a, **k):
        for item in real_iter(*a, **k):
            time.sleep(0.02)  # a slow disk
            yield item

    monkeypatch.setattr(files_mod, "_iter_files", slow_iter)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    t = asyncio.create_task(ticker())
    res = await SearchTextTool().run(SearchTextInput(pattern="needle_3\\b"), _ctx(tmp_path))
    t.cancel()
    assert "needle_3" in res.output
    assert ticks >= 10  # the event loop (the screen) kept running during the search


async def test_deadline_stops_the_search_with_advice(tmp_path: Path, monkeypatch):
    _tree(tmp_path)
    monkeypatch.setattr(files_mod, "SEARCH_DEADLINE_S", 0.0)
    res = await SearchTextTool().run(SearchTextInput(pattern="needle"), _ctx(tmp_path))
    assert res.data.get("timed_out") and "pass path= to search one folder" in res.output
    res = await GlobTool().run(GlobInput(pattern="**/*.py"), _ctx(tmp_path))
    assert res.data["timed_out"] and "narrower pattern" in res.output


async def test_cancel_stops_the_worker(tmp_path: Path, monkeypatch):
    _tree(tmp_path, 200)
    seen = []
    real_iter = files_mod._iter_files

    def slow_iter(*a, **k):
        for item in real_iter(*a, **k):
            seen.append(item)
            time.sleep(0.01)
            yield item

    monkeypatch.setattr(files_mod, "_iter_files", slow_iter)
    task = asyncio.create_task(
        SearchTextTool().run(SearchTextInput(pattern="nothing-matches"), _ctx(tmp_path))
    )
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.1)
    count = len(seen)
    await asyncio.sleep(0.2)
    assert len(seen) == count < 200  # the thread stopped walking


async def test_glob_patterns_still_match(tmp_path: Path):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    for rel in ("a.toml", "src/b.py", "src/pkg/c.py", "src/pkg/d.txt"):
        (tmp_path / rel).write_text("x")
    run = GlobTool().run
    assert (await run(GlobInput(pattern="**/*.py"), _ctx(tmp_path))).output.split() == [
        "src/b.py",
        "src/pkg/c.py",
    ]
    assert (await run(GlobInput(pattern="src/*.py"), _ctx(tmp_path))).output == "src/b.py"
    assert (await run(GlobInput(pattern="*.toml"), _ctx(tmp_path))).output == "a.toml"


def test_home_folder_notice(tmp_path: Path, monkeypatch):
    from trendlab.ui.activity import home_notice

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert "home folder" in home_notice(tmp_path)
    assert home_notice(tmp_path / "proj") == ""
