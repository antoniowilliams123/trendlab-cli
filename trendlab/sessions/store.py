"""SQLite session persistence.

Schema is versioned. Approvals are part of session state so a restart can see
what was pending — and refuse to act on it (see ``ApprovalManager.recover``).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3
_SESSION_COLUMNS = {"parent_id": "TEXT", "branch_point": "INTEGER", "label": "TEXT"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_path TEXT NOT NULL,
    machine TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    model TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,
    tool TEXT NOT NULL,
    category TEXT NOT NULL,
    risk TEXT NOT NULL,
    summary TEXT NOT NULL,
    command TEXT,
    cwd TEXT NOT NULL,
    affected_files TEXT NOT NULL,
    explanation TEXT NOT NULL,
    preview TEXT,
    requested_scope TEXT NOT NULL,
    machine TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    decision TEXT,
    decided_scope TEXT,
    decided_at TEXT,
    decided_via TEXT,
    decided_by TEXT,
    resolution_reason TEXT,
    process_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approvals_session ON approvals(session_id);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    role TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE TABLE IF NOT EXISTS session_state (
    session_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, key)
);
CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    model TEXT NOT NULL,
    role TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    cost_usd REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    label TEXT,
    git_head TEXT,
    task_id TEXT,
    files TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            have = {r["name"] for r in self._conn.execute("PRAGMA table_info(sessions)")}
            for col, typ in _SESSION_COLUMNS.items():  # v3: session branching
                if col not in have:
                    self._conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {typ}")
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- sessions -----------------------------------------------------------
    def create_session(
        self,
        project_path: str,
        machine: str,
        model: str | None = None,
        *,
        parent_id: str | None = None,
        branch_point: int | None = None,
        label: str | None = None,
    ) -> str:
        sid = uuid.uuid4().hex[:12]
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions"
                "(id, project_path, machine, created_at, updated_at, status, model,"
                " parent_id, branch_point, label)"
                " VALUES(?,?,?,?,?, 'active', ?,?,?,?)",
                (sid, project_path, machine, now, now, model, parent_id, branch_point, label),
            )
            self._conn.commit()
        return sid

    def fork_session(
        self,
        source_id: str,
        machine: str,
        model: str | None,
        *,
        upto: int | None = None,
        label: str | None = None,
    ) -> str:
        """Copy a session's conversation (first ``upto`` messages) and state into a new child."""
        src = self.get_session(source_id)
        if src is None:
            raise KeyError(source_id)
        msgs = self.messages(source_id)
        if upto is not None:
            msgs = msgs[:upto]
        new_id = self.create_session(
            src["project_path"],
            machine,
            model or src.get("model"),
            parent_id=source_id,
            branch_point=len(msgs),
            label=label,
        )
        for m in msgs:
            self.append_message(new_id, m)
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM session_state WHERE session_id=?", (source_id,)
            ).fetchall()
            for r in rows:
                self._conn.execute(
                    "INSERT OR REPLACE INTO session_state(session_id, key, value, updated_at)"
                    " VALUES(?,?,?,?)",
                    (new_id, r["key"], r["value"], _now()),
                )
            self._conn.commit()
        return new_id

    def children(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE parent_id=? ORDER BY created_at", (session_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def session_tree(self, project_path: str, limit: int = 200) -> list[tuple[int, dict[str, Any]]]:
        """Sessions of a project as ``(depth, row)`` in tree order (roots oldest first)."""
        rows = self.sessions(project_path, limit=limit)
        by_parent: dict[str | None, list[dict[str, Any]]] = {}
        ids = {r["id"] for r in rows}
        for r in rows:
            parent = r.get("parent_id") if r.get("parent_id") in ids else None
            by_parent.setdefault(parent, []).append(r)
        for lst in by_parent.values():
            lst.sort(key=lambda r: r["created_at"])
        out: list[tuple[int, dict[str, Any]]] = []

        def visit(parent: str | None, depth: int) -> None:
            for r in by_parent.get(parent, []):
                out.append((depth, r))
                visit(r["id"], depth + 1)

        visit(None, 0)
        return out

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def latest_session(self, project_path: str | None = None) -> dict[str, Any] | None:
        q = "SELECT * FROM sessions"
        params: tuple[Any, ...] = ()
        if project_path:
            q += " WHERE project_path=?"
            params = (project_path,)
        q += " ORDER BY updated_at DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(q, params).fetchone()
        return dict(row) if row else None

    def touch_session(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (_now(), session_id))
            self._conn.commit()

    # -- events ---------------------------------------------------------------
    def append_event(
        self, session_id: str | None, type_: str, data: dict[str, Any], ts: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(session_id, ts, type, data) VALUES(?,?,?,?)",
                (session_id, ts, type_, json.dumps(data, default=str)),
            )
            self._conn.commit()

    def events(self, session_id: str, type_: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM events WHERE session_id=?"
        params: list[Any] = [session_id]
        if type_:
            q += " AND type=?"
            params.append(type_)
        q += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"])
            out.append(d)
        return out

    # -- messages / state -------------------------------------------------------
    def append_message(self, session_id: str, message: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages(session_id, ts, role, payload) VALUES(?,?,?,?)",
                (session_id, _now(), message.get("role", "?"), json.dumps(message, default=str)),
            )
            self._conn.commit()

    def search(
        self, text: str, project_path: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Exact (substring) search over stored messages and events (U6)."""
        like = f"%{text}%"
        out: list[dict[str, Any]] = []
        for table, col, kind in (("messages", "payload", "message"), ("events", "data", "event")):
            role_col = "m.role" if table == "messages" else "m.type"
            q = (
                f"SELECT m.session_id, m.ts, {role_col} AS role, m.{col} AS body, s.project_path "
                f"FROM {table} m JOIN sessions s ON s.id = m.session_id WHERE m.{col} LIKE ?"
            )
            params: list[Any] = [like]
            if project_path:
                q += " AND s.project_path = ?"
                params.append(project_path)
            q += " ORDER BY m.id DESC LIMIT ?"
            params.append(limit)
            with self._lock:
                rows = self._conn.execute(q, params).fetchall()
            for r in rows:
                body = r["body"] or ""
                i = max(0, body.lower().find(text.lower()))
                out.append(
                    {
                        "session_id": r["session_id"],
                        "ts": r["ts"],
                        "kind": kind,
                        "role": r["role"],
                        "project": r["project_path"],
                        "snippet": body[max(0, i - 80) : i + 120].replace("\n", " "),
                    }
                )
        out.sort(key=lambda r: r["ts"], reverse=True)
        return out[:limit]

    def ranked_search(
        self, text: str, project_path: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Sessions ranked by relevance (BM25 over each session's messages), not recency."""
        from trendlab.context.retrieval import BM25, tokens

        sessions = self.sessions(project_path, limit=2000)
        docs, meta = [], []
        for s in sessions:
            body = []
            for m in self.messages(s["id"]):
                c = m.get("content")
                if isinstance(c, str) and m.get("role") in {"user", "assistant"}:
                    body.append(c[:2000])
            if body:
                docs.append(tokens(" ".join(body)[:20_000]))
                meta.append((s, body[0]))
        if not docs:
            return []
        scores = BM25(docs).scores(tokens(text))
        ranked = sorted(zip(meta, scores, strict=True), key=lambda ms: -ms[1])
        return [
            {
                "session_id": s["id"],
                "ts": s["updated_at"],
                "project": s["project_path"],
                "score": round(score, 3),
                "first_prompt": first[:160].replace("\n", " "),
            }
            for (s, first), score in ranked[:limit]
            if score > 0
        ]

    def prune(self, *, older_than_days: int, keep_latest: int = 50) -> dict[str, int]:
        """Retention (U6): delete sessions older than the window and their rows, always keeping
        the ``keep_latest`` most recent sessions."""
        from datetime import UTC, datetime, timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        counts = {"sessions": 0, "messages": 0, "events": 0, "model_calls": 0}
        with self._lock:
            keep = {
                r["id"]
                for r in self._conn.execute(
                    "SELECT id FROM sessions ORDER BY updated_at DESC LIMIT ?", (keep_latest,)
                ).fetchall()
            }
            old = [
                r["id"]
                for r in self._conn.execute(
                    "SELECT id FROM sessions WHERE updated_at < ?", (cutoff,)
                ).fetchall()
                if r["id"] not in keep
            ]
            tables = [
                r["name"]
                for r in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            ]
            for sid in old:
                for table in (
                    "messages",
                    "events",
                    "model_calls",
                    "checkpoints",
                    "approvals",
                    "session_state",
                ):
                    if table not in tables:
                        continue
                    cur = self._conn.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
                    if table in counts:
                        counts[table] += cur.rowcount
                self._conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
                counts["sessions"] += 1
            self._conn.commit()
        return counts

    def prune_session(self, session_id: str) -> None:
        """Delete one session and all its rows (re-import, explicit delete)."""
        with self._lock:
            tables = {
                r["name"]
                for r in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for table in (
                "messages",
                "events",
                "model_calls",
                "checkpoints",
                "approvals",
                "session_state",
            ):
                if table in tables:
                    self._conn.execute(f"DELETE FROM {table} WHERE session_id=?", (session_id,))
            self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            self._conn.commit()

    def stats(self, days: int = 7) -> dict[str, Any]:
        """Session analytics (U6): volume, spend, outcomes and failure reasons over a window."""
        from collections import Counter
        from datetime import UTC, datetime, timedelta

        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._lock:
            sessions = self._conn.execute(
                "SELECT id, project_path, model FROM sessions WHERE updated_at >= ?", (since,)
            ).fetchall()
            usage = self._conn.execute(
                "SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd),0) AS cost, "
                "COALESCE(SUM(input_tokens),0) AS inp, COALESCE(SUM(output_tokens),0) AS outp "
                "FROM model_calls WHERE ts >= ?",
                (since,),
            ).fetchone()
            events = self._conn.execute(
                "SELECT type, data FROM events WHERE ts >= ? AND type IN ('run.completed',"
                "'run.failed','guard.fired','tool.skipped','verify.verdict','tool.circuit_opened',"
                "'claim.unsupported','scope.checked','security.injection_suspected',"
                "'invariant.violated','route.decided')",
                (since,),
            ).fetchall()
        outcomes: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        codes: Counter[str] = Counter()
        from trendlab.agent.taxonomy import classify_stop  # local: agent imports sessions

        guards: Counter[str] = Counter()
        skipped: Counter[str] = Counter()
        verdicts: Counter[str] = Counter()
        breakers = 0
        q: Counter[str] = Counter()
        routes: Counter[str] = Counter()
        answers: list[dict[str, Any]] = []
        for e in events:
            d = json.loads(e["data"]) if e["data"] else {}
            t = e["type"]
            if t == "claim.unsupported":
                q["unsupported_claims"] += 1
                continue
            if t == "scope.checked":
                q["scope_checked"] += 1
                q["scope_problems"] += 0 if d.get("ok") else 1
                continue
            if t == "security.injection_suspected":
                q["injection_suspected"] += 1
                continue
            if t == "invariant.violated":
                q["invariant_violations"] += 1
                continue
            if t == "communication.checked":
                answers.append(d)
                continue
            if t == "route.decided":
                routes[str(d.get("kind"))] += 1
                continue
            if t == "run.completed":
                outcomes["completed"] += 1
                q["completed_with_changes"] += 1 if d.get("changed_files") else 0
                q["validated"] += 1 if d.get("changed_files") and d.get("validated") else 0
            elif t == "run.failed":
                outcomes["failed"] += 1
                reasons[str(d.get("stop_reason") or "")[:60]] += 1
                codes[d.get("failure_code") or classify_stop("FAILED", d.get("stop_reason"))] += 1
            elif t == "guard.fired":
                guards[str(d.get("guard"))] += 1
            elif t == "tool.skipped":
                skipped[str(d.get("reason"))] += 1
            elif t == "verify.verdict":
                verdicts[str(d.get("verdict"))] += 1
            else:
                breakers += 1
        return {
            "days": days,
            "sessions": len(sessions),
            "projects": len({s["project_path"] for s in sessions}),
            "models": dict(Counter(s["model"] for s in sessions)),
            "model_calls": usage["calls"],
            "cost_usd": round(usage["cost"], 4),
            "input_tokens": usage["inp"],
            "output_tokens": usage["outp"],
            "runs": dict(outcomes),
            "failure_reasons": dict(reasons.most_common(8)),
            "failure_codes": dict(codes.most_common()),
            "guards": dict(guards.most_common(10)),
            "skipped_tools": dict(skipped.most_common(8)),
            "verifier_verdicts": dict(verdicts),
            "breakers_opened": breakers,
            "routes": dict(routes),
            "communication": _communication(answers),
            "online_quality": {
                # live runs scored from their own evidence (online evaluation)
                "validated_rate": round(q["validated"] / q["completed_with_changes"], 3)
                if q["completed_with_changes"]
                else None,
                "unsupported_claim_rate": round(
                    q["unsupported_claims"] / max(1, outcomes["completed"]), 3
                ),
                "scope_problem_rate": round(q["scope_problems"] / q["scope_checked"], 3)
                if q["scope_checked"]
                else None,
                "injection_suspected": q["injection_suspected"],
                "invariant_violations": q["invariant_violations"],
            },
        }

    def messages(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM messages WHERE session_id=? ORDER BY id", (session_id,)
            ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def clear_messages(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            self._conn.commit()

    def set_state(self, session_id: str, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO session_state(session_id, key, value, updated_at)"
                " VALUES(?,?,?,?)",
                (session_id, key, json.dumps(value, default=str), _now()),
            )
            self._conn.commit()

    def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM session_state WHERE session_id=? AND key=?", (session_id, key)
            ).fetchone()
        return json.loads(row["value"]) if row else default

    def record_model_call(
        self,
        session_id: str,
        model: str,
        role: str,
        input_tokens: int,
        output_tokens: int,
        cached: int,
        latency_ms: int,
        cost_usd: float,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO model_calls(session_id, ts, model, role, input_tokens, output_tokens,"
                " cached_input_tokens, latency_ms, cost_usd) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    _now(),
                    model,
                    role,
                    input_tokens,
                    output_tokens,
                    cached,
                    latency_ms,
                    cost_usd,
                ),
            )
            self._conn.commit()

    def usage(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS input_tokens,"
                " COALESCE(SUM(output_tokens),0) AS output_tokens,"
                " COALESCE(SUM(cost_usd),0) AS cost_usd"
                " FROM model_calls WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return dict(row)

    def sessions(self, project_path: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        q = "SELECT * FROM sessions"
        params: list[Any] = []
        if project_path:
            q += " WHERE project_path=?"
            params.append(project_path)
        q += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def set_session_status(self, session_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET status=?, updated_at=? WHERE id=?",
                (status, _now(), session_id),
            )
            self._conn.commit()

    # -- checkpoints ------------------------------------------------------------
    def insert_checkpoint(self, row: dict[str, Any]) -> None:
        row = dict(row)
        row["files"] = json.dumps(row["files"])
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO checkpoints({cols}) VALUES({marks})", tuple(row.values())
            )
            self._conn.commit()

    def checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM checkpoints WHERE session_id=? ORDER BY created_at", (session_id,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["files"] = json.loads(d["files"])
            out.append(d)
        return out

    def delete_checkpoint(self, checkpoint_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM checkpoints WHERE id=?", (checkpoint_id,))
            self._conn.commit()

    # -- approvals ------------------------------------------------------------
    def insert_approval(self, row: dict[str, Any]) -> None:
        row = dict(row)
        row["affected_files"] = json.dumps(row.get("affected_files", []))
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO approvals({cols}) VALUES({marks})", tuple(row.values())
            )
            self._conn.commit()

    def update_approval(self, approval_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE approvals SET {sets} WHERE id=?", (*fields.values(), approval_id)
            )
            self._conn.commit()

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
        return self._approval_row(row) if row else None

    def approvals(
        self, session_id: str | None = None, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        q = "SELECT * FROM approvals WHERE 1=1"
        params: list[Any] = []
        if session_id:
            q += " AND session_id=?"
            params.append(session_id)
        if status:
            q += " AND status=?"
            params.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [self._approval_row(r) for r in rows]

    def pending_approvals_from_other_processes(self, process_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM approvals WHERE status='pending' AND process_id<>?", (process_id,)
            ).fetchall()
        return [self._approval_row(r) for r in rows]

    @staticmethod
    def _approval_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["affected_files"] = json.loads(d["affected_files"] or "[]")
        return d


def _communication(answers: list[dict[str, Any]]) -> dict[str, Any]:
    """Answer style over the period (U9): watch for communication drift between periods."""
    if not answers:
        return {}
    words = sorted(int(a.get("words") or 0) for a in answers)
    eases = [a["reading_ease"] for a in answers if a.get("reading_ease") is not None]
    return {
        "answers": len(answers),
        "median_words": words[len(words) // 2],
        "reading_ease": round(sum(eases) / len(eases), 1) if eases else None,
        "robospeak_rate": round(sum(1 for a in answers if a.get("robospeak")) / len(answers), 3),
        "constraint_breaks": sum(1 for a in answers if a.get("issues")),
        "rewrites_requested": sum(1 for a in answers if a.get("rewrite_requested")),
    }
