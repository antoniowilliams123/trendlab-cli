"""Inbox (cheap-model spec §7.2): clustered issue cards, SQLite-backed, with the actions the
UI exposes (apply / test / dismiss / silence / open)."""

from __future__ import annotations

import contextlib
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
CREATE TABLE IF NOT EXISTS candidate (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    case_prefix TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,              -- see CANDIDATE_STATUSES
    base_sha TEXT,
    head_sha TEXT,
    branch TEXT,
    worktree TEXT,
    session_id TEXT,
    reserved_usd REAL NOT NULL,        -- reserved at start
    spend_usd REAL,                    -- final, from the call records, at finish or recovery
    charged_usd REAL,                  -- live: booked calls plus in-flight reservations
    day TEXT NOT NULL,                 -- America/New_York date of the start
    started_at TEXT NOT NULL,
    finished_at TEXT,
    files_changed TEXT NOT NULL DEFAULT '[]',
    touches_protected TEXT NOT NULL DEFAULT '[]',
    tests TEXT,
    bundle TEXT,
    manifest_sha256 TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidate_prefix ON candidate(case_prefix);
CREATE TABLE IF NOT EXISTS lesson (
    id TEXT PRIMARY KEY,
    signature TEXT NOT NULL,
    symptom TEXT NOT NULL,
    root_cause_class TEXT,
    fix_pattern TEXT NOT NULL,
    files TEXT NOT NULL DEFAULT '[]',
    evidence TEXT NOT NULL,
    context TEXT,
    succeeded INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
# IL-B candidate statuses (evals spec §30.3, D-110). Only "drafting" is in flight.
CANDIDATE_STATUSES = (
    "drafting",
    "handed_off",
    "tests_failed",
    "module_origin_failed",
    "base_tests_failed",
    "no_change",
    "budget_stop",
    "paused",
    "blackout",
    "timeout",
    "shutdown",
    "error",
    "pruned",
)
CANDIDATE_FIELDS = (
    "base_sha",
    "head_sha",
    "branch",
    "worktree",
    "session_id",
    "files_changed",
    "touches_protected",
    "tests",
    "bundle",
    "manifest_sha256",
    "detail",
)
# Only these sources have deterministic signatures worth keeping; raw run text is never stored.
SIGNATURE_SOURCES = ("evals", "observe")
CASE_SPLITS = ("evals|main|", "evals|regression|")
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


def split_signature(sig: str, case_id: str | None = None) -> tuple[str, str, str] | None:
    """``evals|<split>|<capability>|<case_id>|<codes>`` as ``(left, case_id, codes)``, or None.

    A capability or a failure code may contain ``|``; a case id never does. Given the case id
    from the item's ``evals:<run>:<case_id>`` ref, the cut is made at its single
    ``|<case_id>|``; an ambiguous signature is None (ineligible). Without one, the signature is
    parsed from the right."""
    if not isinstance(sig, str) or not sig.startswith(CASE_SPLITS):
        return None
    if case_id is None:
        parts = sig.rsplit("|", 2)
        if len(parts) != 3:
            return None
        left, case_id, codes = parts
    else:
        if not case_id or "|" in case_id:
            return None
        needle = f"|{case_id}|"
        start = sig.find(needle)
        if start < 0 or sig.find(needle, start + 1) >= 0:
            return None
        left, codes = sig[:start], sig[start + len(needle) :]
    head = next(p for p in CASE_SPLITS if sig.startswith(p))
    if not case_id or not codes or len(left) <= len(head) or not left.startswith(head):
        return None
    return left, case_id, codes


class Inbox:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            added = self._migrate_locked()
            self._migrate_candidate_locked()
        if added:
            self._backfill_signatures()

    def _migrate_locked(self) -> bool:
        """Add ``issue.signature`` to an older database. True when this process added it."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(issue)")}
        if "signature" in cols:
            return False
        try:
            self._conn.execute("ALTER TABLE issue ADD COLUMN signature TEXT")
            self._conn.commit()
        except sqlite3.OperationalError:  # a concurrent process added it first
            return False
        return True

    def _migrate_candidate_locked(self) -> None:
        """Add ``candidate.charged_usd`` to a database made before it existed."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(candidate)")}
        if "charged_usd" not in cols:
            with contextlib.suppress(sqlite3.OperationalError):  # a concurrent process did it
                self._conn.execute("ALTER TABLE candidate ADD COLUMN charged_usd REAL")
                self._conn.commit()

    def _backfill_signatures(self) -> None:
        """Recover the signatures of evals items stored before the column existed, from the
        validated payloads of ingested notices. Items no notice names stay NULL (ineligible)."""
        from trendlab.engine.observe import validate_notice  # observe imports this module

        with self._lock:
            notices = self._conn.execute(
                "SELECT client, report_payload FROM notice WHERE status='ingested' "
                "AND report_payload IS NOT NULL"
            ).fetchall()
        by_cluster: dict[str, str] = {}
        for row in notices:
            try:
                entries = validate_notice(json.loads(row["report_payload"]), row["client"])
            except Exception:  # noqa: BLE001 — one odd stored payload never blocks the rest
                continue
            for e in entries:
                by_cluster[e["cluster"]] = e["signature"]
        with self._lock:
            try:
                for key, sig in by_cluster.items():
                    self._conn.execute(
                        "UPDATE issue SET signature=? WHERE source='evals' AND signature IS NULL "
                        "AND cluster_key=?",
                        (sig, key),
                    )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

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
        cluster: str | None = None,
    ) -> dict[str, Any]:
        """Insert or merge into the cluster; silenced clusters stay silenced but count.
        ``cluster`` names the cluster explicitly instead of hashing the signature."""
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
                cluster=cluster,
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
        stored_sig = signature if source in SIGNATURE_SOURCES else None
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
                "card=COALESCE(?, card), signature=COALESCE(signature, ?) WHERE id=?",
                (
                    now,
                    json.dumps(refs[-20:]),
                    json.dumps(files),
                    status,
                    root_cause,
                    card_json,
                    stored_sig,
                    row["id"],
                ),
            )
            return row["id"]
        issue_id = hashlib.sha1(f"{project}:{key}:{now}".encode()).hexdigest()[:10]
        self._conn.execute(
            "INSERT INTO issue(id, project, cluster_key, title, root_cause, impacted_files, "
            "evidence_refs, first_seen, last_seen, occurrences, severity, status, source, card, "
            "signature) VALUES(?,?,?,?,?,?,?,?,?,1,?,'open',?,?,?)",
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
                stored_sig,
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

    # -- IL-B candidates and lessons (evals spec §30.3, §30.5, D-110) ----------------------------
    def start_candidate(
        self,
        *,
        item_id: str,
        case_prefix: str,
        day: str,
        base_sha: str,
        reserved_usd: float,
        worktree: str,
        branch: str,
    ) -> dict[str, Any]:
        """A new ``drafting`` row that reserves ``reserved_usd``; refused while any row is
        drafting (one draft at a time). The id is ``cand-<item>-<n>``, ``n`` the attempt."""
        with self._lock:
            try:
                if self._conn.execute("SELECT 1 FROM candidate WHERE status='drafting'").fetchone():
                    raise RuntimeError("a candidate is already drafting")
                n = self._prefix_attempts_locked(case_prefix) + 1
                cid = f"cand-{item_id}-{n}"
                self._conn.execute(
                    "INSERT INTO candidate(id, item_id, case_prefix, attempt, status, base_sha, "
                    "branch, worktree, reserved_usd, day, started_at) "
                    "VALUES(?,?,?,?,'drafting',?,?,?,?,?,?)",
                    (
                        cid,
                        item_id,
                        case_prefix,
                        n,
                        base_sha,
                        branch,
                        worktree,
                        float(reserved_usd),
                        day,
                        _now(),
                    ),
                )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return self.candidate(cid)

    def set_candidate_session(self, cid: str, session_id: str) -> None:
        self._update_candidate(cid, {"session_id": session_id})

    def set_candidate_charge(self, cid: str, usd: float) -> None:
        """The draft's live charge (booked calls plus in-flight reservations), kept current so
        crash recovery never records less than was possibly billed."""
        self._update_candidate(cid, {"charged_usd": float(usd)})

    def finish_candidate(
        self, cid: str, *, status: str, spend_usd: float | None, **fields: Any
    ) -> None:
        """The final status and spend (which replaces the reservation), plus any other field."""
        if status not in CANDIDATE_STATUSES or status == "drafting":
            raise ValueError(f"unknown final status {status!r}")
        sets = {"status": status, "spend_usd": spend_usd, "finished_at": _now()}
        self._update_candidate(cid, {**sets, **fields})

    def mark_candidate(self, cid: str, *, status: str, **fields: Any) -> None:
        """A status change after the finish (``pruned``); the spend stays as recorded."""
        if status not in CANDIDATE_STATUSES or status == "drafting":
            raise ValueError(f"unknown status {status!r}")
        self._update_candidate(cid, {"status": status, **fields})

    def _update_candidate(self, cid: str, sets: dict[str, Any]) -> None:
        allowed = set(CANDIDATE_FIELDS) | {"status", "spend_usd", "finished_at", "charged_usd"}
        extra = sorted(k for k in sets if k not in allowed)
        if extra:
            raise ValueError(f"unknown candidate fields: {', '.join(extra)}")
        values = [json.dumps(v) if isinstance(v, dict | list) else v for v in sets.values()]
        with self._lock:
            try:
                cur = self._conn.execute(
                    "UPDATE candidate SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?",
                    (*values, cid),
                )
                if cur.rowcount != 1:
                    raise KeyError(cid)
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def candidate(self, cid: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM candidate WHERE id=?", (cid,)).fetchone()
        if row is None:
            raise KeyError(cid)
        return self._candidate_row(row)

    def candidates(self, *, status: str | None = None) -> list[dict[str, Any]]:
        q, params = "SELECT * FROM candidate", []
        if status:
            q += " WHERE status=?"
            params.append(status)
        with self._lock:
            rows = self._conn.execute(q + " ORDER BY started_at, id", params).fetchall()
        return [self._candidate_row(r) for r in rows]

    def drafting_rows(self) -> list[dict[str, Any]]:
        return self.candidates(status="drafting")

    def _prefix_attempts_locked(self, case_prefix: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM candidate WHERE case_prefix=?", (case_prefix,)
        ).fetchone()
        return int(row["n"])

    def prefix_attempts(self, case_prefix: str) -> int:
        with self._lock:
            return self._prefix_attempts_locked(case_prefix)

    def _spend(self, where: str, params: tuple, exclude: str | None = None) -> float:
        if exclude is not None:  # every other row (a running draft checks its own calls)
            where, params = f"({where}) AND id != ?", (*params, exclude)
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(COALESCE(spend_usd, reserved_usd)), 0) AS usd "
                f"FROM candidate WHERE {where}",
                params,
            ).fetchone()
        return float(row["usd"])

    def prefix_spend(self, case_prefix: str, exclude: str | None = None) -> float:
        return self._spend("case_prefix=?", (case_prefix,), exclude)

    def spent_today(self, day: str, exclude: str | None = None) -> float:
        return self._spend("day=?", (day,), exclude)

    def spent_month(self, month: str, exclude: str | None = None) -> float:
        """Month-to-date spend for ``YYYY-MM`` (days are America/New_York dates)."""
        return self._spend("substr(day, 1, 7)=?", (month,), exclude)

    def prefix_has_handoff(self, case_prefix: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM candidate WHERE case_prefix=? AND status='handed_off'",
                (case_prefix,),
            ).fetchone()
        return row is not None

    def last_attempt_at(self, case_prefix: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(started_at) AS at FROM candidate WHERE case_prefix=?", (case_prefix,)
            ).fetchone()
        return row["at"]

    def pending_handoffs(self, consumed: set[str] | frozenset[str] = frozenset()) -> int:
        """Hand-offs that are not pruned and not consumed by the referee (its own file)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM candidate WHERE status='handed_off'"
            ).fetchall()
        return sum(1 for r in rows if r["id"] not in consumed)

    def lessons_for(self, case_prefix: str, limit: int = 3) -> list[dict[str, Any]]:
        """Active lessons for the same split and capability, best ``succeeded - failed`` first.
        Read only in IL-B: lessons are written from referee verdicts (IL-C)."""
        scope = case_prefix.rsplit("|", 1)[0] + "|"  # a case id never contains "|"
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM lesson WHERE status='active' AND substr(signature, 1, ?)=? "
                "ORDER BY (succeeded - failed) DESC, updated_at DESC LIMIT ?",
                (len(scope), scope, limit),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["files"] = json.loads(d["files"] or "[]")
            out.append(d)
        return out

    @staticmethod
    def _candidate_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for key in ("files_changed", "touches_protected"):
            d[key] = json.loads(d[key] or "[]")
        if d.get("tests"):
            try:
                d["tests"] = json.loads(d["tests"])
            except ValueError:
                pass
        return d

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
