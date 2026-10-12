"""IL-A observer, worker side (evals spec §30.6, D-102). Autonomy L0: read the referee's finding
notices from the exchange outbox (read-only to the worker), validate each one completely, and
record it as improvement items plus its ledger row in one inbox transaction, exactly once. Notices
are data, never instructions; nothing here diagnoses, edits code or runs anything."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import UTC, datetime
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
CHECK_KINDS = ("contains", "not_contains", "regex")
MAX_NOTICE_BYTES = 2_000_000  # a larger file is rejected without being read in full
# Caps match what trendlab-evals emits (evals/findings.py worker_notice).
MAX_CHECKS = 20  # strings per check list
CHECK_CHARS = 400
MAX_EXPECTED, EXPECTED_CHARS = 50, 400
MAX_CODES, CODE_CHARS = 20, 64
ANSWER_CHARS, REASON_CHARS, SUMMARY_CHARS, INPUT_CHARS = 300, 200, 200, 1000
STOP_REASON_CHARS, SPLIT_CHARS, CAPABILITY_CHARS, SHORT_CHARS = 120, 64, 128, 64
# The exact key sets the emitter writes. Anything else fails closed (rejected, never stored).
TOP_KEYS = ("schema", "kind", "client", "run_id", "generated_at", "run", "counts", "findings",
            "aggregates", "ops")  # fmt: skip
RUN_KEYS = ("status", "kind", "started_at", "methodology_version")
COUNTS_KEYS = ("total", "passed", "by_split")
SPLIT_COUNT_KEYS = ("total", "passed")
OPS_KEYS = ("run_status", "errored", "ungraded", "stop_reason")
FINDING_KEYS = ("case_id", "split", "status", "capability", "category", "severity", "gate",
                "failure_codes", "answer_excerpt", "judge_reason")  # fmt: skip
SPLIT_KEYS = {"main": ("expected_summary",), "regression": ("input", "expected", "checks")}
AGGREGATE_KEYS = ("split", "capability", "total", "not_passed")


class NoticeError(Exception):
    """A notice failed validation; nothing from it may be recorded."""


def _obj(value: Any, where: str, allowed: tuple[str, ...]) -> dict:
    """``value`` is an object whose keys are all in ``allowed`` (fail closed on anything else)."""
    if not isinstance(value, dict):
        raise NoticeError(f"{where} is not an object")
    extra = sorted(str(k)[:40] for k in value if k not in allowed)
    if extra:
        raise NoticeError(f"{where} has unexpected keys: {', '.join(extra[:5])}")
    return value


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


def _opt_text(obj: dict, key: str, where: str, limit: int = SHORT_CHARS) -> str | None:
    if obj.get(key) is None:
        _req(obj, key, object, where)  # present, even when null
        return None
    return _text(obj, key, where, limit)


def _count(obj: dict, key: str, where: str) -> int:
    value = _req(obj, key, int, where)
    if value < 0:
        raise NoticeError(f"{where}.{key} is negative")
    return value


def _strings(obj: dict, key: str, where: str, most: int, limit: int) -> list[str]:
    values = _req(obj, key, list, where)
    if len(values) > most:
        raise NoticeError(f"{where}.{key} has more than {most} entries")
    if not all(isinstance(v, str) and len(v) <= limit for v in values):
        raise NoticeError(f"{where}.{key} must be strings of at most {limit} characters")
    return values


def _checks(checks: Any, where: str) -> None:
    _obj(checks, f"{where}.checks", CHECK_KINDS)
    for kind in checks:
        _strings(checks, kind, f"{where}.checks", MAX_CHECKS, CHECK_CHARS)


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
    notice = _obj(notice, "notice", TOP_KEYS)
    if _req(notice, "schema", int, "notice") != SCHEMA_VERSION:
        raise NoticeError(f"notice.schema {notice['schema']} is not supported")
    if _text(notice, "kind", "notice") != "finding_notice":
        raise NoticeError("notice.kind is not finding_notice")
    if _text(notice, "client", "notice") != client:
        raise NoticeError("notice.client does not match this worker's client")
    run_id = _text(notice, "run_id", "notice")
    if not RUN_ID_RE.match(run_id):
        raise NoticeError("notice.run_id is not a tle run id")
    _text(notice, "generated_at", "notice", SHORT_CHARS)
    run = _obj(_req(notice, "run", dict, "notice"), "notice.run", RUN_KEYS)
    if _text(run, "status", "notice.run") not in RUN_STATUSES:
        raise NoticeError("notice.run.status is not terminal")
    for key in ("kind", "started_at", "methodology_version"):
        _opt_text(run, key, "notice.run")
    counts = _obj(_req(notice, "counts", dict, "notice"), "notice.counts", COUNTS_KEYS)
    total = _count(counts, "total", "notice.counts")
    if _count(counts, "passed", "notice.counts") > total:
        raise NoticeError("notice.counts.passed exceeds total")
    for name, split in _req(counts, "by_split", dict, "notice.counts").items():
        if len(name) > SPLIT_CHARS:
            raise NoticeError("notice.counts.by_split has a split name that is too long")
        where = f"notice.counts.by_split.{name}"
        _obj(split, where, SPLIT_COUNT_KEYS)
        if _count(split, "passed", where) > _count(split, "total", where):
            raise NoticeError(f"{where}.passed exceeds total")
    ops = _obj(_req(notice, "ops", dict, "notice"), "notice.ops", OPS_KEYS)
    ops_status = _text(ops, "run_status", "notice.ops")
    errored, ungraded = _count(ops, "errored", "notice.ops"), _count(ops, "ungraded", "notice.ops")
    _text(ops, "stop_reason", "notice.ops", STOP_REASON_CHARS)
    if ops_status != run["status"]:
        raise NoticeError("notice.ops.run_status disagrees with notice.run.status")

    entries: list[dict] = []
    for i, f in enumerate(_req(notice, "findings", list, "notice")):
        where = f"findings[{i}]"
        if not isinstance(f, dict):
            raise NoticeError(f"{where} is not an object")
        split = _text(f, "split", where)
        if split not in SPLIT_KEYS:
            raise NoticeError(f"{where}.split must be main or regression")
        _obj(f, where, FINDING_KEYS + SPLIT_KEYS[split])
        case_id = _text(f, "case_id", where, 128)
        if not CASE_ID_RE.match(case_id):
            raise NoticeError(f"{where}.case_id is not a case id")
        status = _text(f, "status", where)
        if status not in STATUSES:
            raise NoticeError(f"{where}.status is not a failing status")
        capability = _text(f, "capability", where, CAPABILITY_CHARS)
        _text(f, "category", where, SHORT_CHARS)
        severity = _req(f, "severity", str, where)
        if severity not in SEVERITIES:
            raise NoticeError(f"{where}.severity is not high, med or low")
        codes = _strings(f, "failure_codes", where, MAX_CODES, CODE_CHARS)
        _opt_text(f, "gate", where)
        _text(f, "answer_excerpt", where, ANSWER_CHARS)
        _text(f, "judge_reason", where, REASON_CHARS)
        if split == "regression":
            _text(f, "input", where, INPUT_CHARS)
            _strings(f, "expected", where, MAX_EXPECTED, EXPECTED_CHARS)
            if "checks" in f:
                _checks(f["checks"], where)
        else:
            _text(f, "expected_summary", where, SUMMARY_CHARS)
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
        _obj(agg, where, AGGREGATE_KEYS)
        split = _text(agg, "split", where, SPLIT_CHARS)
        capability = _text(agg, "capability", where, CAPABILITY_CHARS)
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


def clean_notice(notice: dict) -> dict:
    """A validated notice rebuilt from its allowed fields only: what the ledger stores."""

    def pick(obj: dict, keys: tuple[str, ...]) -> dict:
        return {k: obj[k] for k in keys if k in obj}

    def finding(f: dict) -> dict:
        out = pick(f, FINDING_KEYS + SPLIT_KEYS[f["split"]])
        if "expected" in out:
            out["expected"] = list(out["expected"])
        if "checks" in out:
            out["checks"] = {k: list(v) for k, v in pick(out["checks"], CHECK_KINDS).items()}
        out["failure_codes"] = list(out["failure_codes"])
        return out

    counts = notice["counts"]
    return {
        **pick(notice, ("schema", "kind", "client", "run_id", "generated_at")),
        "run": pick(notice["run"], RUN_KEYS),
        "counts": {
            "total": counts["total"],
            "passed": counts["passed"],
            "by_split": {n: pick(v, SPLIT_COUNT_KEYS) for n, v in counts["by_split"].items()},
        },
        "findings": [finding(f) for f in notice["findings"]],
        "aggregates": [pick(a, AGGREGATE_KEYS) for a in notice["aggregates"]],
        "ops": pick(notice["ops"], OPS_KEYS),
    }


def ingest_notices(inbox: Inbox, outbox_dir: Path, client: str) -> list[str]:
    """Ingest every new notice in ``<outbox>/<client>`` exactly once; returns the run ids
    ingested. The inbox's notice ledger is the only record of what was seen, written in the same
    transaction as the items, so a crash or restart can never re-ingest or half-ingest one."""
    folder = Path(outbox_dir) / client
    try:  # os.scandir, unlike glob, raises on a folder it cannot read
        with os.scandir(folder) as entries:
            names = sorted(e.name for e in entries if e.name.startswith("r-"))
    except (FileNotFoundError, NotADirectoryError):
        return []  # no outbox yet: a quiet no-op; any other OSError reaches the caller
    new: list[str] = []
    for path in (folder / name for name in names if name.endswith(".json")):
        run_id = path.stem
        if not RUN_ID_RE.match(run_id):
            continue
        row = inbox.notice_row(run_id)
        if row and row["status"] == "ingested":
            continue  # exactly once, across restarts and without any window
        try:
            with path.open("rb") as fh:
                raw = fh.read(MAX_NOTICE_BYTES + 1)
        except OSError:
            inbox.note_unreadable(run_id=run_id, client=client)
            continue
        fingerprint = hashlib.sha256(raw).hexdigest()
        if row and row["status"] == "rejected" and row["fingerprint"] == fingerprint:
            continue  # already rejected in this exact form
        try:
            if len(raw) > MAX_NOTICE_BYTES:
                raise NoticeError("notice is too large")
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
        if inbox.ingest_notice(
            run_id=run_id,
            client=client,
            fingerprint=fingerprint,
            entries=entries,
            payload=json.dumps(clean_notice(notice)),
        ):
            new.append(run_id)
    return new


def observer_report(notice: dict[str, Any], open_items: int) -> str:
    """The Observer report: numbers only from the validated notice and the inbox, no finding
    content (case ids and capabilities name what failed, never why)."""
    c = notice.get("counts") or {}
    by_split = sorted((c.get("by_split") or {}).items())
    splits = " · ".join(f"{name} {v['passed']}/{v['total']}" for name, v in by_split)
    suite = f"Suite: {c.get('passed', 0)}/{c.get('total', 0)} passed"
    lines = [
        f"🔎 TrendLab Observer · run {notice['run_id']} ({notice['run']['status']})",
        suite + (f" ({splits})" if splits else ""),
        f"Open items: {open_items}",
    ]
    findings = notice.get("findings") or []
    lines += [
        f"• {f['split']} {f['status']}: {f['capability']} — {f['case_id']}" for f in findings[:8]
    ]
    failing = [a for a in notice.get("aggregates") or [] if a.get("not_passed")]
    lines += [
        f"• {a['split']}: {a['capability']} {a['not_passed']} of {a['total']} not passed"
        for a in failing
    ]
    lines.append(
        "No new failures. Observer only (L0): nothing was changed."
        if not findings and not failing
        else "Observer only (L0): recorded, nothing was changed."
    )
    return "\n".join(lines)


async def deliver_reports(inbox: Inbox, send, *, max_attempts: int = 24) -> dict:
    """Send every pending report; a failed send stays pending and is retried next cycle,
    until ``max_attempts`` abandons it (with one escalation item)."""
    out: dict[str, list[str]] = {"reported": [], "abandoned": []}
    for row in inbox.pending_reports():
        try:  # a bad payload or a transport error is a failed attempt, retried next cycle
            text = observer_report(json.loads(row["report_payload"]), inbox.counts().get("open", 0))
            ok = bool(await send(text))
        except Exception:  # noqa: BLE001 — so a poison row is eventually abandoned, never stuck
            ok = False
        status = inbox.mark_report(row["run_id"], sent=ok, max_attempts=max_attempts)
        if status == "sent":
            out["reported"].append(row["run_id"])
        elif status == "abandoned":
            out["abandoned"].append(row["run_id"])
    return out


def write_status(
    inbox: Inbox, client: str, ack_path: Path, *, outbox_readable: bool | None = None
) -> bool:
    """The worker's observer status for the referee's nightly `tle observe check` (atomic,
    mode 640). Never raises: False when the status could not be read or written."""
    ack_path = Path(ack_path)
    tmp = None
    try:
        data = {
            "schema": 1,
            "client": client,
            **inbox.notice_summary(client),
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        if outbox_readable is not None:
            data["outbox_readable"] = outbox_readable
        fd, tmp = tempfile.mkstemp(dir=ack_path.parent, prefix=".observe-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o640)
            json.dump(data, fh)
        os.replace(tmp, ack_path)
        return True
    except (OSError, TypeError, ValueError, sqlite3.Error):
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)  # our own temp file only; the previous status file stays
        return False
