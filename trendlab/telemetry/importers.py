"""Cross-harness trace normalisation (uplift U6).

Imports sessions recorded by other agent harnesses into TrendLab's session store, in TrendLab's
own schema (sessions · messages in OpenAI chat shape · events · model_calls), so `trendlab
search`, `trendlab stats`, `trendlab trace`, the meta-loop and the replay all work across
harnesses. Supported: Claude Code JSONL transcripts (``~/.claude/projects/<dir>/<id>.jsonl``).

Imports are idempotent: the source session id is recorded in session_state and a second import
of the same file is skipped unless the file grew (then the session is replaced).
"""

from __future__ import annotations

import json
import socket
import uuid
from pathlib import Path
from typing import Any

SOURCE_KEY = "imported_from"


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(str(b.get("text") or ""))
    return "\n".join(p for p in parts if p)


def _tool_result_text(block: dict[str, Any]) -> str:
    c = block.get("content")
    if isinstance(c, list):
        return "\n".join(str(x.get("text") or "") for x in c if isinstance(x, dict))
    return str(c or "")


def parse_claude_code(path: Path) -> dict[str, Any]:
    """Normalise one Claude Code transcript. Returns {source_id, project, started, ended,
    model, messages, events, model_calls}."""
    messages: list[dict[str, Any]] = []
    events: list[tuple[str, str, dict[str, Any]]] = []
    calls: list[dict[str, Any]] = []
    source_id, project, started, ended = path.stem, "", "", ""
    models: dict[str, int] = {}
    pending: dict[str, tuple[str, str]] = {}  # tool_use_id -> (name, ts)
    seen_requests: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        t = d.get("type")
        if t not in {"user", "assistant"} or d.get("isSidechain"):
            continue
        ts = str(d.get("timestamp") or "")
        source_id = str(d.get("sessionId") or source_id)
        project = project or str(d.get("cwd") or "")
        started = started or ts
        ended = ts or ended
        m = d.get("message") or {}
        content = m.get("content")
        if t == "user":
            if d.get("isMeta"):
                continue
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        tid = str(b.get("tool_use_id") or "")
                        name, t0 = pending.pop(tid, ("?", ts))
                        out = _tool_result_text(b)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tid,
                                "name": name,
                                "content": out[:20_000],
                                "_ts": ts,
                            }
                        )
                        events.append(
                            (
                                "tool.completed",
                                ts,
                                {
                                    "tool": name,
                                    "ok": not b.get("is_error"),
                                    "lines": out.count("\n") + 1,
                                    "error": out[:200] if b.get("is_error") else "",
                                    "harness": "claude-code",
                                },
                            )
                        )
            text = _text_of(content)
            if text and not text.startswith("<"):
                messages.append({"role": "user", "content": text, "_ts": ts})
            continue
        # assistant
        model = str(m.get("model") or "")
        if model:
            models[model] = models.get(model, 0) + 1
        text = _text_of(content)
        tool_calls = []
        for b in content or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tid = str(b.get("id") or uuid.uuid4().hex[:8])
                name = str(b.get("name") or "?")
                pending[tid] = (name, ts)
                args = b.get("input") or {}
                tool_calls.append(
                    {
                        "id": tid,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)[:20_000]},
                    }
                )
                detail = (
                    args.get("command")
                    or args.get("file_path")
                    or args.get("path")
                    or args.get("pattern")
                    or ""
                )
                events.append(
                    (
                        "tool.started",
                        ts,
                        {"tool": name, "detail": str(detail)[:200], "harness": "claude-code"},
                    )
                )
        if text or tool_calls:
            msg: dict[str, Any] = {"role": "assistant", "content": text, "_ts": ts}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            messages.append(msg)
        u = m.get("usage") or {}
        rid = str(d.get("requestId") or m.get("id") or "")
        if u and rid not in seen_requests:  # one API call can be split across several records
            seen_requests.add(rid)
            cached = int(u.get("cache_read_input_tokens") or 0)
            calls.append(
                {
                    "ts": ts,
                    "model": f"anthropic:{model}" if model else "anthropic:?",
                    "input_tokens": int(u.get("input_tokens") or 0)
                    + cached
                    + int(u.get("cache_creation_input_tokens") or 0),
                    "output_tokens": int(u.get("output_tokens") or 0),
                    "cached": cached,
                }
            )
    top = max(models, key=models.get) if models else ""
    return {
        "source_id": source_id,
        "project": project,
        "started": started,
        "ended": ended,
        "model": f"anthropic:{top}" if top else "",
        "messages": messages,
        "events": events,
        "model_calls": calls,
        "size": path.stat().st_size,
    }


def import_claude_code(store, path: Path, *, pricing=None) -> dict[str, Any]:
    """Write one transcript into ``store``. ``pricing`` is an optional
    callable(model, input_tokens, output_tokens, cached) -> usd."""
    data = parse_claude_code(path)
    if not data["messages"]:
        return {"file": path.name, "skipped": "no messages"}
    conn = store._conn  # noqa: SLF001 — importer writes with source timestamps
    with store._lock:  # noqa: SLF001
        row = conn.execute(
            "SELECT session_id, value FROM session_state WHERE key=? AND value LIKE ?",
            (SOURCE_KEY, f'%"{data["source_id"]}"%'),
        ).fetchone()
    if row is not None:
        prev = json.loads(row["value"])
        if prev.get("size") == data["size"]:
            return {"file": path.name, "skipped": "already imported", "session": row["session_id"]}
        store.prune_session(row["session_id"])
    sid = "cc" + uuid.uuid4().hex[:10]
    with store._lock:  # noqa: SLF001
        conn.execute(
            "INSERT INTO sessions(id, project_path, machine, created_at, updated_at, status, model,"
            " parent_id, branch_point, label) VALUES(?,?,?,?,?, 'imported', ?, NULL, NULL, ?)",
            (
                sid,
                data["project"],
                socket.gethostname(),
                data["started"],
                data["ended"],
                data["model"],
                "claude-code",
            ),
        )
        for msg in data["messages"]:
            ts = msg.pop("_ts", data["started"])
            conn.execute(
                "INSERT INTO messages(session_id, ts, role, payload) VALUES(?,?,?,?)",
                (sid, ts, msg["role"], json.dumps(msg, default=str)),
            )
        for typ, ts, payload in data["events"]:
            conn.execute(
                "INSERT INTO events(session_id, ts, type, data) VALUES(?,?,?,?)",
                (sid, ts, typ, json.dumps(payload, default=str)),
            )
        cost_total = 0.0
        for c in data["model_calls"]:
            cost = (
                pricing(c["model"], c["input_tokens"], c["output_tokens"], c["cached"])
                if pricing
                else 0.0
            )
            cost_total += cost
            conn.execute(
                "INSERT INTO model_calls(session_id, ts, model, role, input_tokens, output_tokens,"
                " cached_input_tokens, latency_ms, cost_usd) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    c["ts"],
                    c["model"],
                    "main",
                    c["input_tokens"],
                    c["output_tokens"],
                    c["cached"],
                    0,
                    cost,
                ),
            )
        conn.execute(
            "INSERT OR REPLACE INTO session_state(session_id, key, value, updated_at)"
            " VALUES(?,?,?,?)",
            (
                sid,
                SOURCE_KEY,
                json.dumps(
                    {
                        "harness": "claude-code",
                        "source_id": data["source_id"],
                        "file": str(path),
                        "size": data["size"],
                    }
                ),
                data["ended"],
            ),
        )
        conn.commit()
    return {
        "file": path.name,
        "session": sid,
        "messages": len(data["messages"]),
        "tool_calls": sum(1 for e in data["events"] if e[0] == "tool.started"),
        "model_calls": len(data["model_calls"]),
        "cost_usd": round(cost_total, 4),
    }
