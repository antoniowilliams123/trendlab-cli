"""Uplift U6: spans and trace tree, OTel export, session search, analytics, retention."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trendlab.config.schema import PermissionMode
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.sessions.store import SessionStore
from trendlab.telemetry.events import EventBus, EventRecorder, EventType
from trendlab.telemetry.spans import render_tree, to_otel, trace_tree

from .test_agent_runtime import make_agent


def _records(rec: EventRecorder) -> list[dict]:
    return [{"event": e.type.value, "ts": e.timestamp.isoformat(), **e.data} for e in rec.events]


async def test_spans_nest_and_concurrent_children_get_own_ids():
    bus = EventBus()
    rec = EventRecorder()
    bus.subscribe(rec)

    async def child(n):
        with bus.span("tool:x", "s", n=n):
            bus.emit(EventType.TOOL_STARTED, "s", tool="x")
            await asyncio.sleep(0.01)

    with bus.span("run", "s"):
        await asyncio.gather(child(1), child(2))
    started = [e for e in rec.events if e.type == EventType.SPAN_STARTED]
    run_id = started[0].data["span_id"]
    kids = [e for e in started[1:]]
    assert len(kids) == 2 and all(k.data["parent_id"] == run_id for k in kids)
    assert kids[0].data["span_id"] != kids[1].data["span_id"]
    tool_events = [e for e in rec.events if e.type == EventType.TOOL_STARTED]
    assert {e.data["span_id"] for e in tool_events} == {k.data["span_id"] for k in kids}
    roots = trace_tree(_records(rec))
    assert len(roots) == 1 and roots[0]["name"] == "run" and len(roots[0]["children"]) == 2
    assert roots[0]["duration_ms"] is not None and roots[0]["children"][0]["events"] == 1
    lines = render_tree(roots)
    assert lines[0].startswith("run") and lines[1].startswith("  tool:x")


async def test_agent_run_produces_a_full_trace(project: Path, manager_factory, events):
    rec = EventRecorder()
    events.subscribe(rec)
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="a", name="read_file", arguments={"path": "README.md"}),
                    ToolCall(id="b", name="list_directory", arguments={"path": "."}),
                ]
            ),
            ModelResponse(text="The README says demo."),
        ]
    )
    agent, _ = make_agent(project, manager_factory(), events, provider, mode=PermissionMode.UNSAFE)
    await agent.run("what does the readme say?")
    roots = trace_tree(_records(rec))
    run = roots[0]
    names = [c["name"] for c in run["children"]]
    assert run["name"] == "run" and names.count("model_call") == 2 and "tool_batch" in names
    batch = next(c for c in run["children"] if c["name"] == "tool_batch")
    assert sorted(c["name"] for c in batch["children"]) == ["tool:list_directory", "tool:read_file"]
    otel = to_otel("abc123", roots)
    spans = otel["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == 6 and all(
        len(s["spanId"]) == 16 and len(s["traceId"]) == 32 for s in spans
    )
    assert sum(1 for s in spans if not s["parentSpanId"]) == 1
    json.dumps(otel)


def test_store_search_stats_and_prune(tmp_path: Path):
    store = SessionStore(tmp_path / "s.db")
    old = store.create_session("/p", "m")
    new = store.create_session("/p", "m")
    old_id = old["id"] if isinstance(old, dict) else store.sessions("/p")[1]["id"]
    new_id = new["id"] if isinstance(new, dict) else store.sessions("/p")[0]["id"]
    now = datetime.now(UTC).isoformat()
    store.append_message(old_id, {"role": "user", "content": "fix the flaky parser please"})
    store.append_message(new_id, {"role": "user", "content": "add a CSV exporter"})
    store.append_event(new_id, "run.failed", {"stop_reason": "max_iterations (40) reached"}, now)
    store.append_event(new_id, "guard.fired", {"guard": "announced_action"}, now)
    hits = store.search("flaky parser")
    assert (
        len(hits) == 1 and hits[0]["session_id"] == old_id and "flaky parser" in hits[0]["snippet"]
    )
    assert store.search("max_iterations")[0]["kind"] == "event"
    st = store.stats(7)
    assert (
        st["sessions"] == 2
        and st["runs"] == {"failed": 1}
        and st["guards"] == {"announced_action": 1}
    )
    assert "max_iterations (40) reached" in st["failure_reasons"]
    stale = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    store._conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (stale, old_id))  # noqa: SLF001
    store._conn.commit()  # noqa: SLF001
    assert store.prune(older_than_days=90, keep_latest=5) == {
        "sessions": 0,
        "messages": 0,
        "events": 0,
        "model_calls": 0,
    }
    counts = store.prune(older_than_days=90, keep_latest=1)
    assert counts["sessions"] == 1 and counts["messages"] == 1
    assert store.get_session(old_id) is None and store.get_session(new_id) is not None
    store.close()


def test_claude_code_import_normalises_and_is_idempotent(tmp_path: Path):
    from trendlab.telemetry.importers import import_claude_code

    lines = [
        {"type": "mode", "sessionId": "abc"},
        {
            "type": "user",
            "sessionId": "abc",
            "cwd": "/proj",
            "timestamp": "2026-10-01T10:00:00Z",
            "message": {"role": "user", "content": "fix the parser"},
        },
        {
            "type": "assistant",
            "sessionId": "abc",
            "cwd": "/proj",
            "timestamp": "2026-10-01T10:00:05Z",
            "requestId": "r1",
            "message": {
                "model": "claude-x",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Reading it."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": "/proj/p.py"},
                    },
                ],
                "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5},
            },
        },
        {
            "type": "user",
            "sessionId": "abc",
            "timestamp": "2026-10-01T10:00:06Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "def parse(): ...",
                        "is_error": False,
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "isSidechain": True,
            "sessionId": "abc",
            "timestamp": "x",
            "message": {"content": [{"type": "text", "text": "ignored"}]},
        },
    ]
    f = tmp_path / "abc.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    store = SessionStore(tmp_path / "s.db")
    r = import_claude_code(store, f, pricing=lambda m, i, o, c: 0.001)
    assert (
        r["messages"] == 3
        and r["tool_calls"] == 1
        and r["model_calls"] == 1
        and r["cost_usd"] == 0.001
    )
    sid = r["session"]
    msgs = store.messages(sid)
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool"]
    assert msgs[1]["tool_calls"][0]["function"]["name"] == "Read" and msgs[2]["name"] == "Read"
    evs = store.events(sid)
    assert [e["type"] for e in evs] == ["tool.started", "tool.completed"] and evs[0]["data"][
        "detail"
    ] == "/proj/p.py"
    sess = store.get_session(sid)
    assert (
        sess["project_path"] == "/proj"
        and sess["model"] == "anthropic:claude-x"
        and sess["created_at"].startswith("2026-10-01")
    )
    assert store.usage(sid)["input_tokens"] == 100
    assert import_claude_code(store, f)["skipped"] == "already imported"
    assert store.search("fix the parser")[0]["session_id"] == sid
    # the file grew: re-import replaces the old session
    with f.open("a") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": "abc",
                    "timestamp": "2026-10-01T10:01:00Z",
                    "message": {"role": "user", "content": "thanks"},
                }
            )
            + "\n"
        )
    r2 = import_claude_code(store, f)
    assert r2["messages"] == 4 and store.get_session(sid) is None
    store.close()


def test_dashboard_and_export(tmp_path: Path):
    import csv
    import html.parser

    from trendlab.engine.inbox import Inbox
    from trendlab.telemetry.dashboard import collect, export, render_html

    store = SessionStore(tmp_path / "s.db")
    sid = store.create_session("/p", "m", "deepseek:deepseek-flash")
    now = datetime.now(UTC).isoformat()
    store.append_event(
        sid, "tool.completed", {"tool": "run_tests", "ok": False, "error": "1 failed"}, now
    )
    store.append_event(
        sid, "tool.completed", {"tool": "web_fetch", "ok": False, "error": "network error"}, now
    )
    store.append_event(sid, "tool.completed", {"tool": "read_file", "ok": True}, now)
    store.append_event(sid, "run.completed", {}, now)
    store.record_model_call(sid, "deepseek:deepseek-flash", "main", 100, 10, 0, 5, 0.002)
    inbox = Inbox(tmp_path / "i.db")
    inbox.record(project="/p", title="broken thing", signature="x", severity="high")
    hist = tmp_path / "canary.jsonl"
    hist.write_text(
        json.dumps(
            {"at": "2026-10-08T03:30", "passes": 1.0, "cost": 0.1, "passes_ci95": [0.8, 1.0]}
        )
        + "\n"
    )
    data = collect(store, 7, hist, inbox)
    assert (
        data["tools"]["run_tests"]["success"] == 1.0
    )  # a failing test is a result, not a tool error
    assert data["tools"]["web_fetch"]["success"] == 0.0 and data["harnesses"] == {"trendlab": 1}
    page = render_html(data)
    assert (
        page.startswith("<!doctype html>")
        and "broken thing" in page
        and "prefers-color-scheme" in page
    )

    class P(html.parser.HTMLParser):
        tags = 0

        def handle_starttag(self, t, a):
            P.tags += 1

    P().feed(page)
    assert P.tags > 50
    counts = export(store, tmp_path / "out", messages=True)
    assert counts["sessions"] == 1 and counts["model_calls"] == 1 and counts["events"] == 4
    rows = list(csv.reader((tmp_path / "out/model_calls.csv").open()))
    assert rows[0][:3] == ["id", "session_id", "ts"] and rows[1][3] == "deepseek:deepseek-flash"
    first_event = json.loads((tmp_path / "out/events.jsonl").read_text().splitlines()[0])
    assert isinstance(first_event["data"], dict)
    inbox.close()
    store.close()
