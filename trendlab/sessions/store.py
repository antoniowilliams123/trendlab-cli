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

SCHEMA_VERSION = 1

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
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- sessions -----------------------------------------------------------
    def create_session(self, project_path: str, machine: str, model: str | None = None) -> str:
        sid = uuid.uuid4().hex[:12]
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions"
                "(id, project_path, machine, created_at, updated_at, status, model)"
                " VALUES(?,?,?,?,?, 'active', ?)",
                (sid, project_path, machine, now, now, model),
            )
            self._conn.commit()
        return sid

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
