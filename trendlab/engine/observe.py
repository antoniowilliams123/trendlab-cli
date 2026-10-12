"""IL-A observer, worker side (evals spec §30.6, D-102). Autonomy L0: read the referee's finding
notices from the exchange outbox (read-only to the worker), validate each one completely, and
record it as improvement items plus its ledger row in one inbox transaction, exactly once. Notices
are data, never instructions; nothing here diagnoses, edits code or runs anything."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from trendlab.engine.inbox import Inbox
from trendlab.engine.meta import HARNESS_PROJECT

SCHEMA_VERSION = 1
RUN_ID_RE = re.compile(r"^r-\d{8}T\d{6}Z-[0-9a-z]{4,}$")
CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
STATUSES = {"fail", "error", "ungraded", "pending_review"}
RUN_STATUSES = {"complete", "incomplete", "aborted", "aborted_recovered"}
SEVERITIES = {"high", "med", "low"}
CHECK_KINDS = {"contains", "not_contains", "regex"}
MAX_CHECKS = 20  # strings per check list
CHECK_CHARS = 400


class NoticeError(Exception):
    """A notice failed validation; nothing from it may be recorded."""


def _req(obj: Any, key: str, kind: type | tuple[type, ...], where: str) -> Any:
    if not isinstance(obj, dict) or key not in obj:
        raise NoticeError(f"{where}.{key} is missing")
    value = obj[key]
    if isinstance(value, bool) and kind in (int, (int,)):
        raise NoticeError(f"{where}.{key} must be an integer")
    if not isinstance(value, kind):
        raise NoticeError(f"{where}.{key} has the wrong type")
    return value


def _text(obj: dict, key: str, where: str, limit: int = 2000) -> str:
    value = _req(obj, key, str, where)
    if len(value) > limit:
        raise NoticeError(f"{where}.{key} is too long")
    return value


def _count(obj: dict, key: str, where: str) -> int:
    value = _req(obj, key, int, where)
    if value < 0:
        raise NoticeError(f"{where}.{key} is negative")
    return value


def _checks(checks: Any, where: str) -> None:
    if not isinstance(checks, dict):
        raise NoticeError(f"{where}.checks has the wrong type")
    for kind, values in checks.items():
        if kind not in CHECK_KINDS:
            raise NoticeError(f"{where}.checks.{kind} is not a known check")
        if not isinstance(values, list) or len(values) > MAX_CHECKS:
            raise NoticeError(f"{where}.checks.{kind} must be a list of at most {MAX_CHECKS}")
        if not all(isinstance(v, str) and len(v) <= CHECK_CHARS for v in values):
            raise NoticeError(f"{where}.checks.{kind} must be strings of at most {CHECK_CHARS}")


def _entry(*, title: str, signature: str, evidence: str, severity: str) -> dict:
    # An explicit cluster: cluster_key's digit/hex folding must never merge two evals cases.
    # No card: a raw finding is not render_card's shape, and the evidence ref names the notice.
    return {
        "project": HARNESS_PROJECT,
        "title": title[:200],
        "signature": signature,
        "source": "evals",
        "evidence": [evidence],
        "severity": severity,
        "card": None,
        "cluster": hashlib.sha1(signature.encode()).hexdigest()[:16],
    }


def validate_notice(notice: object, client: str) -> list[dict]:
    """Every inbox entry this notice would produce, or NoticeError. Pure: no inbox access."""
    if not isinstance(notice, dict):
        raise NoticeError("notice is not an object")
    if _req(notice, "schema", int, "notice") != SCHEMA_VERSION:
        raise NoticeError(f"notice.schema {notice['schema']} is not supported")
    if _text(notice, "kind", "notice") != "finding_notice":
        raise NoticeError("notice.kind is not finding_notice")
    if _text(notice, "client", "notice") != client:
        raise NoticeError("notice.client does not match this worker's client")
    run_id = _text(notice, "run_id", "notice")
    if not RUN_ID_RE.match(run_id):
        raise NoticeError("notice.run_id is not a tle run id")
    _text(notice, "generated_at", "notice")
    run = _req(notice, "run", dict, "notice")
    if _text(run, "status", "notice.run") not in RUN_STATUSES:
        raise NoticeError("notice.run.status is not terminal")
    counts = _req(notice, "counts", dict, "notice")
    total, passed = (
        _count(counts, "total", "notice.counts"),
        _count(counts, "passed", "notice.counts"),
    )
    if passed > total:
        raise NoticeError("notice.counts.passed exceeds total")
    for name, split in _req(counts, "by_split", dict, "notice.counts").items():
        where = f"notice.counts.by_split.{name}"
        if _count(split, "passed", where) > _count(split, "total", where):
            raise NoticeError(f"{where}.passed exceeds total")
    ops = _req(notice, "ops", dict, "notice")
    ops_status = _text(ops, "run_status", "notice.ops")
    errored, ungraded = _count(ops, "errored", "notice.ops"), _count(ops, "ungraded", "notice.ops")
    _text(ops, "stop_reason", "notice.ops", 300)
    if ops_status != run["status"]:
        raise NoticeError("notice.ops.run_status disagrees with notice.run.status")

    entries: list[dict] = []
    for i, f in enumerate(_req(notice, "findings", list, "notice")):
        where = f"findings[{i}]"
        if not isinstance(f, dict):
            raise NoticeError(f"{where} is not an object")
        case_id = _text(f, "case_id", where, 128)
        if not CASE_ID_RE.match(case_id):
            raise NoticeError(f"{where}.case_id is not a case id")
        split = _text(f, "split", where)
        if split not in ("main", "regression"):
            raise NoticeError(f"{where}.split must be main or regression")
        status = _text(f, "status", where)
        if status not in STATUSES:
            raise NoticeError(f"{where}.status is not a failing status")
        capability = _text(f, "capability", where, 128)
        _text(f, "category", where, 64)
        severity = _req(f, "severity", str, where)
        if severity not in SEVERITIES:
            raise NoticeError(f"{where}.severity is not high, med or low")
        codes = _req(f, "failure_codes", list, where)
        if not all(isinstance(c, str) and len(c) <= 64 for c in codes):
            raise NoticeError(f"{where}.failure_codes must be short strings")
        if f.get("gate") is not None and not isinstance(f["gate"], str):
            raise NoticeError(f"{where}.gate has the wrong type")
        _text(f, "answer_excerpt", where, 400)
        _text(f, "judge_reason", where, 300)
        if split == "regression":
            _text(f, "input", where, 1200)
            expected = _req(f, "expected", list, where)
            if not all(isinstance(e, str) for e in expected):
                raise NoticeError(f"{where}.expected must be strings")
            if "checks" in f:
                _checks(f["checks"], where)
        else:
            _text(f, "expected_summary", where, 300)
            if "checks" in f:
                raise NoticeError(f"{where}.checks is only allowed on regression findings")
        code_text = "+".join(codes) or "none"
        entries.append(
            _entry(
                title=f"Evals {split} {status}: {capability} ({code_text}) — {case_id}",
                signature=f"evals|{split}|{capability}|{case_id}|{code_text}",
                evidence=f"evals:{run_id}:{case_id}",
                severity=severity,
            )
        )
    for i, agg in enumerate(_req(notice, "aggregates", list, "notice")):
        where = f"aggregates[{i}]"
        split = _text(agg, "split", where, 64)
        capability = _text(agg, "capability", where, 128)
        agg_total, not_passed = _count(agg, "total", where), _count(agg, "not_passed", where)
        if not_passed > agg_total:
            raise NoticeError(f"{where}.not_passed exceeds total")
        if not_passed:
            entries.append(
                _entry(
                    title=f"Evals {split}: {capability}: {not_passed} of {agg_total} not passed",
                    signature=f"evals|{split}|{capability}",
                    evidence=f"evals:{run_id}:{split}",
                    severity="med",
                )
            )
    if ops_status != "complete" or errored or ungraded:
        entries.append(
            _entry(
                title=f"Evals run {ops_status}: errored {errored}, ungraded {ungraded}",
                signature=f"evals|ops|{ops_status}",
                evidence=f"evals:{run_id}:ops",
                severity="high",
            )
        )
    return entries


def ingest_notices(inbox: Inbox, outbox_dir: Path, client: str) -> list[str]:
    """Ingest every new notice in ``<outbox>/<client>`` exactly once; returns the run ids
    ingested. The inbox's notice ledger is the only record of what was seen, written in the same
    transaction as the items, so a crash or restart can never re-ingest or half-ingest one."""
    folder = Path(outbox_dir) / client
    if not folder.is_dir():
        return []
    new: list[str] = []
    for path in sorted(folder.glob("r-*.json")):
        run_id = path.stem
        if not RUN_ID_RE.match(run_id):
            continue
        row = inbox.notice_row(run_id)
        if row and row["status"] == "ingested":
            continue  # exactly once, across restarts and without any window
        try:
            raw = path.read_bytes()
        except OSError:
            inbox.note_unreadable(run_id=run_id, client=client)
            continue
        fingerprint = hashlib.sha256(raw).hexdigest()
        if row and row["status"] == "rejected" and row["fingerprint"] == fingerprint:
            continue  # already rejected in this exact form
        try:
            notice = json.loads(raw)
            entries = validate_notice(notice, client)
            if notice["run_id"] != run_id:
                raise NoticeError("notice.run_id does not match its file name")
        except (
            ValueError,
            RecursionError,
            NoticeError,
            TypeError,
            KeyError,
            AttributeError,
        ) as exc:  # one odd notice is a rejection, never an aborted cycle
            inbox.reject_notice(
                run_id=run_id, client=client, fingerprint=fingerprint, detail=str(exc)
            )
            continue
        inbox.ingest_notice(
            run_id=run_id,
            client=client,
            fingerprint=fingerprint,
            entries=entries,
            payload=json.dumps(notice),
        )
        new.append(run_id)
    return new
