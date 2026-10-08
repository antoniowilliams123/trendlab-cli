"""Uplift U29: live monitoring renders meaningful events from any session."""

from datetime import UTC, datetime
from pathlib import Path

from trendlab.sessions.store import SessionStore
from trendlab.telemetry.tail import fetch, line


def test_fetch_follows_and_lines_skip_noise(tmp_path: Path):
    store = SessionStore(tmp_path / "s.db")
    now = datetime.now(UTC).isoformat()
    a = store.create_session("/p", "m", "m")
    b = store.create_session("/q", "m", "m")
    store.append_event(a, "tool.started", {"tool": "shell", "command": "pytest -q"}, now)
    store.append_event(a, "model.token", {"text": "x"}, now)
    store.append_event(
        b,
        "run.failed",
        {"stop_reason": "cost limit $1 reached", "failure_code": "BUDGET_COST"},
        now,
    )
    evs = fetch(store, 0)
    lines = [ln for ln in (line(e) for e in evs) if ln]
    assert len(lines) == 2 and "→ shell pytest -q" in lines[0]
    assert "RUN FAILED BUDGET_COST" in lines[1]
    only_b = fetch(store, 0, session=b)
    assert [e["type"] for e in only_b] == ["run.failed"]
    assert fetch(store, evs[-1]["id"]) == []  # nothing new after the cursor
    store.close()
