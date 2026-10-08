"""Inbox (cheap-model spec §7.2): clustered issue cards, SQLite-backed, with the actions the
UI exposes (apply / test / dismiss / silence / open)."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS issue (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    cluster_key TEXT NOT NULL,
    title TEXT NOT NULL,
    root_cause TEXT,
    impacted_files TEXT NOT NULL DEFAULT '[]',
    evidence_refs TEXT NOT NULL DEFAULT '[]',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    severity TEXT NOT NULL DEFAULT 'med',
    status TEXT NOT NULL DEFAULT 'open',
    source TEXT NOT NULL DEFAULT 'run',
    proposed_patch_ref TEXT,
    verification_ref TEXT,
    feedback TEXT,
    card TEXT
);
CREATE INDEX IF NOT EXISTS idx_issue_project ON issue(project, status);
"""
STATUSES = ("open", "applied", "tested", "dismissed", "silenced", "fixed")
_WS = re.compile(r"\s+")
_HEX = re.compile(r"0x[0-9a-f]+|[0-9a-f]{8,}", re.I)
_NUM = re.compile(r"\d+")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def cluster_key(text: str) -> str:
    """Normalised signature: failing-test ids and stack frames cluster across runs even when
    line numbers, addresses and counts differ."""
    t = text.strip().splitlines()
    sig = " ".join(ln.strip() for ln in t if ln.strip())[:600]
    sig = _HEX.sub("#", sig)
    sig = _NUM.sub("N", sig)
    sig = _WS.sub(" ", sig).lower()
    return hashlib.sha1(sig.encode()).hexdigest()[:16]


class Inbox:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # -- writing -----------------------------------------------------------------------------
    def record(
        self,
        *,
        project: str,
        title: str,
        signature: str,
        source: str = "run",
        root_cause: str | None = None,
        impacted_files: list[str] | None = None,
        evidence: list[str] | None = None,
        severity: str = "med",
    ) -> dict[str, Any]:
        """Insert or merge into the cluster; silenced clusters stay silenced but count."""
        key = cluster_key(signature)
        now = _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM issue WHERE project=? AND cluster_key=?", (project, key)
            ).fetchone()
            if row:
                refs = list(json.loads(row["evidence_refs"]))
                for e in evidence or []:
                    if e not in refs:
                        refs.append(e)
                files = sorted(set(json.loads(row["impacted_files"])) | set(impacted_files or []))
                status = row["status"]
                if status in {"dismissed", "fixed", "applied", "tested"}:
                    status = "open"  # it came back
                self._conn.execute(
                    "UPDATE issue SET last_seen=?, occurrences=occurrences+1, evidence_refs=?, "
                    "impacted_files=?, status=?, root_cause=COALESCE(?, root_cause) WHERE id=?",
                    (now, json.dumps(refs[-20:]), json.dumps(files), status, root_cause, row["id"]),
                )
                self._conn.commit()
                found = row["id"]
                issue_id = None
            else:
                found = None
                issue_id = hashlib.sha1(f"{project}:{key}:{now}".encode()).hexdigest()[:10]
        if found is not None:
            return self.get(found)
        assert issue_id is not None
        with self._lock:
            self._conn.execute(
                "INSERT INTO issue(id, project, cluster_key, title, root_cause, impacted_files, "
                "evidence_refs, first_seen, last_seen, occurrences, severity, status, source) "
                "VALUES(?,?,?,?,?,?,?,?,?,1,?,'open',?)",
                (
                    issue_id,
                    project,
                    key,
                    title.strip()[:200],
                    root_cause,
                    json.dumps(sorted(set(impacted_files or []))),
                    json.dumps(list(evidence or [])[:20]),
                    now,
                    now,
                    severity,
                    source,
                ),
            )
            self._conn.commit()
        return self.get(issue_id)

    def set_status(self, issue_id: str, status: str, *, feedback: str | None = None) -> bool:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE issue SET status=?, feedback=COALESCE(?, feedback), last_seen=? WHERE id=?",
                (status, feedback, _now(), issue_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def attach(self, issue_id: str, **fields: Any) -> None:
        allowed = {"proposed_patch_ref", "verification_ref", "card", "root_cause", "feedback"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE issue SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?",
                (
                    *[json.dumps(v) if isinstance(v, dict | list) else v for v in sets.values()],
                    issue_id,
                ),
            )
            self._conn.commit()

    # -- reading -----------------------------------------------------------------------------
    def get(self, issue_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM issue WHERE id=?", (issue_id,)).fetchone()
        if row is None:
            raise KeyError(issue_id)
        return self._row(row)

    def find(self, prefix: str, project: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            q = "SELECT * FROM issue WHERE id LIKE ?"
            params: list[Any] = [prefix + "%"]
            if project:
                q += " AND project=?"
                params.append(project)
            rows = self._conn.execute(q, params).fetchall()
        return self._row(rows[0]) if len(rows) == 1 else None

    def list(
        self, project: str | None = None, *, status: str | None = "open", limit: int = 50
    ) -> list[dict[str, Any]]:
        q = "SELECT * FROM issue WHERE 1=1"
        params: list[Any] = []
        if project:
            q += " AND project=?"
            params.append(project)
        if status:
            q += " AND status=?"
            params.append(status)
        q += (
            " ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'med' THEN 1 ELSE 2 END,"
            " occurrences DESC, last_seen DESC LIMIT ?"
        )
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [self._row(r) for r in rows]

    def counts(self, project: str | None = None) -> dict[str, int]:
        q = "SELECT status, COUNT(*) AS n FROM issue"
        params: list[Any] = []
        if project:
            q += " WHERE project=?"
            params.append(project)
        q += " GROUP BY status"
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return {r["status"]: r["n"] for r in rows}

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["impacted_files"] = json.loads(d["impacted_files"] or "[]")
        d["evidence_refs"] = json.loads(d["evidence_refs"] or "[]")
        if d.get("card"):
            try:
                d["card"] = json.loads(d["card"])
            except ValueError:
                pass
        return d


def digest(issues: list[dict[str, Any]], counts: dict[str, int], project: str = "") -> str:
    """One Telegram-sized digest: counts plus the top three cards (§7.2)."""
    head = f"Inbox{(' · ' + Path(project).name) if project else ''}: " + ", ".join(
        f"{n} {s}" for s, n in sorted(counts.items())
    )
    lines = [head or "Inbox: empty"]
    for it in issues[:3]:
        files = ", ".join(it["impacted_files"][:3]) or "—"
        lines.append(
            f"• [{it['severity']}] {it['title'][:90]} ×{it['occurrences']} · {files}"
            f" · id {it['id']}"
        )
        if it.get("root_cause"):
            lines.append(f"   ↳ {str(it['root_cause'])[:140]}")
    return "\n".join(lines)
