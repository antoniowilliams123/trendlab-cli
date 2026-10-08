"""Uplift U28: every harness prompt is registered, locked and tied to the eval measuring it."""

from pathlib import Path

from trendlab.prompts.registry import LOCK, PROMPTS, table


def test_every_prompt_is_locked_and_measured():
    rows = table()
    assert len(rows) == len(PROMPTS) >= 15
    changed = [r["name"] for r in rows if r["changed"] or r["new"]]
    assert not changed, (
        f"prompts changed without re-running their eval: {changed}; re-run the eval named in "
        "trendlab/prompts/registry.py, then `trendlab prompts --lock`"
    )
    root = LOCK.parent.parent
    for r in rows:
        assert r["eval"], r["name"]
        if r["eval"].startswith("tests/"):
            assert (root / r["eval"].split()[0]).is_file(), r["eval"]
    assert all(r["tokens"] > 20 for r in rows)


def test_lock_file_is_committed():
    assert Path(LOCK).is_file()
