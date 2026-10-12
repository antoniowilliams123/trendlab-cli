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
CREATE TABLE IF NOT EXISTS notice (
    run_id TEXT PRIMARY KEY,
    client TEXT NOT NULL,
    status TEXT NOT NULL,              -- ingested | rejected | unreadable
    fingerprint TEXT,
    detail TEXT,
    read_failures INTEGER NOT NULL DEFAULT 0,
    report_status TEXT,                -- pending | sent | abandoned (ingested notices only)
    report_payload TEXT,               -- the validated notice JSON, used to build the report
    report_attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""
UNREADABLE_THRESHOLD = 3  # failed reads before a notice is escalated
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
        card: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert or merge into the cluster; silenced clusters stay silenced but count."""
        with self._lock:
            issue_id = self._record_locked(
                project=project,
                title=title,
                signature=signature,
                source=source,
                root_cause=root_cause,
                impacted_files=impacted_files,
                evidence=evidence,
                severity=severity,
                card=card,
            )
            self._conn.commit()
        return self.get(issue_id)

    def _record_locked(
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
        card: dict[str, Any] | None = None,
        cluster: str | None = None,
    ) -> str:
        """The body of ``record``. The caller holds ``self._lock`` and owns the transaction:
        no lock, no commit and no public method call here."""
        key = cluster or cluster_key(signature)
        now = _now()
        card_json = json.dumps(card) if card is not None else None
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
                "impacted_files=?, status=?, root_cause=COALESCE(?, root_cause), "
                "card=COALESCE(?, card) WHERE id=?",
                (
                    now,
                    json.dumps(refs[-20:]),
                    json.dumps(files),
                    status,
                    root_cause,
                    card_json,
                    row["id"],
                ),
            )
            return row["id"]
        issue_id = hashlib.sha1(f"{project}:{key}:{now}".encode()).hexdigest()[:10]
        self._conn.execute(
            "INSERT INTO issue(id, project, cluster_key, title, root_cause, impacted_files, "
            "evidence_refs, first_seen, last_seen, occurrences, severity, status, source, card) "
            "VALUES(?,?,?,?,?,?,?,?,?,1,?,'open',?,?)",
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
                card_json,
            ),
        )
        return issue_id

    # -- notice ledger (IL-A observer, evals spec §30.6) -----------------------------------------
    def _system_item(self, *, run_id: str, title: str, cluster: str, ref: str) -> None:
        from trendlab.engine.meta import HARNESS_PROJECT  # meta imports this module

        self._record_locked(
            project=HARNESS_PROJECT,
            title=title,
            signature=cluster,
            source="observe",
            evidence=[f"evals:{run_id}:{ref}"],
            severity="med",
            cluster=cluster,
        )

    def notice_row(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM notice WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def ingest_notice(
        self,
        *,
        run_id: str,
        client: str,
        fingerprint: str,
        entries: list[dict[str, Any]],
        payload: str,
    ) -> bool:
        """Every item of a validated notice plus its ``ingested`` ledger row: one transaction.
        False (and no write) when the notice is already ingested, so a racing caller cannot
        count its items twice."""
        with self._lock:
            done = self._conn.execute(
                "SELECT 1 FROM notice WHERE run_id=? AND status='ingested'", (run_id,)
            ).fetchone()
            if done:
                return False
            try:
                for entry in entries:
                    self._record_locked(**entry)
                self._conn.execute(
                    "INSERT OR REPLACE INTO notice(run_id, client, status, fingerprint, detail, "
                    "read_failures, report_status, report_payload, report_attempts, updated_at) "
                    "VALUES(?,?,'ingested',?,NULL,0,'pending',?,0,?)",
                    (run_id, client, fingerprint, payload, _now()),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return True

    def reject_notice(self, *, run_id: str, client: str, fingerprint: str, detail: str) -> None:
        """One "rejected" item and a ``rejected`` ledger row (fingerprinted): one transaction."""
        with self._lock:
            try:
                self._system_item(
                    run_id=run_id,
                    title="Evals notice rejected (malformed or unsupported)",
                    cluster="evals-notice-rejected",
                    ref="notice",
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO notice(run_id, client, status, fingerprint, detail, "
                    "read_failures, report_status, report_payload, report_attempts, updated_at) "
                    "VALUES(?,?,'rejected',?,?,0,NULL,NULL,0,?)",
                    (run_id, client, fingerprint, detail[:200], _now()),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def note_unreadable(
        self, *, run_id: str, client: str, threshold: int = UNREADABLE_THRESHOLD
    ) -> bool:
        """Count a failed read; at ``threshold`` failures record one "unreadable" item (True)."""
        with self._lock:
            try:
                now = _now()
                row = self._conn.execute(
                    "SELECT read_failures FROM notice WHERE run_id=?", (run_id,)
                ).fetchone()
                if row is None:
                    failures = 1
                    self._conn.execute(
                        "INSERT INTO notice(run_id, client, status, read_failures, updated_at) "
                        "VALUES(?,?,'unreadable',1,?)",
                        (run_id, client, now),
                    )
                else:
                    failures = row["read_failures"] + 1
                    self._conn.execute(
                        "UPDATE notice SET read_failures=?, updated_at=? WHERE run_id=?",
                        (failures, now, run_id),
                    )
                escalate = failures == threshold
                if escalate:
                    self._system_item(
                        run_id=run_id,
                        title="Evals notice unreadable by the worker",
                        cluster="evals-notice-unreadable",
                        ref="notice",
                    )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return escalate

    def pending_reports(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM notice WHERE report_status='pending' ORDER BY run_id"
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_report(self, run_id: str, *, sent: bool, max_attempts: int) -> str:
        """Record one delivery attempt; abandon (with one escalation item) at ``max_attempts``."""
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT report_status, report_attempts FROM notice WHERE run_id=?", (run_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(run_id)
                status = row["report_status"]
                if status != "pending":
                    return status
                attempts = row["report_attempts"]
                if sent:
                    status = "sent"
                else:
                    attempts += 1
                    if attempts >= max_attempts:
                        status = "abandoned"
                        self._system_item(
                            run_id=run_id,
                            title="Observer report undeliverable after retries",
                            cluster="observe-report-undeliverable",
                            ref="report",
                        )
                self._conn.execute(
                    "UPDATE notice SET report_status=?, report_attempts=?, updated_at=? "
                    "WHERE run_id=?",
                    (status, attempts, _now(), run_id),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return status

    def notice_summary(self, client: str) -> dict[str, Any]:
        """The ledger as the referee sees it (the ack file): the last 20 run ids per list."""

        def last(where: str) -> list[str]:
            rows = self._conn.execute(
                f"SELECT run_id FROM notice WHERE client=? AND {where} "
                "ORDER BY run_id DESC LIMIT 20",
                (client,),
            ).fetchall()
            return [r["run_id"] for r in reversed(rows)]

        with self._lock:
            top = self._conn.execute(
                "SELECT MAX(run_id) AS run_id FROM notice WHERE client=? AND status='ingested'",
                (client,),
            ).fetchone()
            return {
                "last_observed_run": top["run_id"],
                "pending_reports": last("report_status='pending'"),
                "abandoned_reports": last("report_status='abandoned'"),
                "unreadable_notices": last(f"read_failures >= {UNREADABLE_THRESHOLD}"),
                "rejected_notices": last("status='rejected'"),
            }

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
        self,
        project: str | None = None,
        *,
        status: str | None = "open",
        limit: int = 50,
        exclude_source: str | None = None,
    ) -> list[dict[str, Any]]:
        q = "SELECT * FROM issue WHERE 1=1"
        params: list[Any] = []
        if exclude_source:  # filtered before the limit, so excluded items never crowd it out
            q += " AND source!=?"
            params.append(exclude_source)
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


_PLAIN_TITLES = (
    ("test failing:", "A test is failing:"),
    ("verifier rejected:", "The reviewer rejected a change for the task:"),
    ("runs fail with", "Runs keep failing with"),
)


def plain_digest(issues: list[dict[str, Any]], limit: int = 5) -> str:
    """A short, plain-English Telegram check-in listing only the issues given (new ones)."""
    n = len(issues)
    lines = [f"TrendLab check-in: {n} new thing{'s' if n != 1 else ''} to look at."]
    for it in issues[:limit]:
        title = str(it["title"])
        for raw, plain in _PLAIN_TITLES:
            if title.lower().startswith(raw):
                title = plain + title[len(raw) :]
                break
        files = it.get("impacted_files") or []
        where = f" ({Path(files[0]).name})" if files else ""
        lines.append(f"• {title[:100]}{where}")
    if n > limit:
        lines.append(f"…and {n - limit} more.")
    lines.append("Details: run `trendlab inbox` on the server.")
    return "\n".join(lines)
