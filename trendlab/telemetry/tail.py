"""Live session monitoring (uplift U29): follow events as they are written.

Reads the session store directly, so it shows any run on the machine — an interactive session
in another terminal, a ``-p`` run, engine jobs — without attaching to the process. One line per
meaningful event; noisy ones (tokens, spans, cost ticks) are skipped.
"""

from __future__ import annotations

import json
from typing import Any

SKIP = {
    "model.token",
    "span.started",
    "span.ended",
    "cost.updated",
    "tool.output",
    "agent.state_changed",
    "context.budget",
    "tool.requested",
    "permission.requested",
}


def fetch(store, after_id: int, session: str | None = None, limit: int = 500) -> list[dict]:
    q = "SELECT id, session_id, ts, type, data FROM events WHERE id > ?"
    params: list[Any] = [after_id]
    if session:
        q += " AND session_id = ?"
        params.append(session)
    q += " ORDER BY id LIMIT ?"
    params.append(limit)
    with store._lock:  # noqa: SLF001 — read-only follow
        rows = store._conn.execute(q, params).fetchall()  # noqa: SLF001
    return [dict(r) for r in rows]


def line(ev: dict[str, Any]) -> str | None:
    t = ev["type"]
    if t in SKIP:
        return None
    d = json.loads(ev["data"]) if ev.get("data") else {}
    when = str(ev["ts"])[11:19]
    sid = str(ev.get("session_id") or "")[:8]
    if t == "tool.started":
        what = d.get("command") or ", ".join(d.get("files") or []) or ""
        body = f"→ {d.get('tool')} {str(what)[:80]}"
    elif t == "tool.completed":
        body = f"{'✓' if d.get('ok', True) else '✗'} {d.get('tool')} {d.get('duration_ms', '')}ms"
        if d.get("error"):
            body += f" {str(d['error'])[:80]}"
    elif t == "model.call_completed":
        body = (
            f"model {d.get('model')} {d.get('input_tokens')}→{d.get('output_tokens')} tok "
            f"${d.get('cost_usd', 0):.4f}"
        )
    elif t in {"run.completed", "run.failed", "run.canceled"}:
        body = f"RUN {t.split('.')[1].upper()}" + (
            f" {d.get('failure_code') or ''} {str(d.get('stop_reason') or '')[:60]}"
            if t != "run.completed"
            else f" ${d.get('cost_usd', 0)}"
        )
    elif t == "verify.verdict":
        body = f"verifier: {d.get('verdict')} ({len(d.get('findings') or [])} findings)"
    elif t == "guard.fired":
        body = f"guard {d.get('guard')}"
    elif t == "recovery.action":
        body = f"recovery {d.get('failure')} → {d.get('action')}"
    elif t == "file.changed":
        body = f"edited {d.get('path') or ', '.join(d.get('files') or [])}"
    elif t == "route.decided":
        body = f"route {d.get('kind')} ({d.get('by')})"
    else:
        body = t
    return f"{when} {sid} {body}"
