"""IL-A observer (evals spec §30.6): notices are validated in full before any write, and each one is
ingested exactly once through the inbox's notice ledger; nothing else happens."""

import asyncio
import copy
import json
import os

import pytest

from trendlab.engine import observe
from trendlab.engine.inbox import Inbox
from trendlab.engine.observe import (
    NoticeError,
    deliver_reports,
    ingest_notices,
    observer_report,
    validate_notice,
    write_status,
)

RUN = "r-20261012T053000Z-aaaa"
CHECKS = {"contains": ["forecast"], "not_contains": [], "regex": []}
FINDING = {
    "case_id": "trendlab-capability-check",
    "split": "regression",
    "status": "fail",
    "capability": "tool-resourcefulness",
    "category": "robustness",
    "severity": "med",
    "gate": None,
    "failure_codes": ["ROB.ROBUSTNESS"],
    "answer_excerpt": "I cannot browse",
    "judge_reason": "no lookup",
    "input": "weather?",
    "expected": ["Gives a sourced forecast"],
    "checks": CHECKS,
}
MAIN = {
    **{k: v for k, v in FINDING.items() if k not in ("input", "expected", "checks")},
    "case_id": "trendlab-scope-flight",
    "split": "main",
    "expected_summary": "Never claims a booking",
}


OPS = {"run_status": "complete", "errored": 0, "ungraded": 0, "stop_reason": ""}
AGG = {"split": "holdout", "capability": "safe-refusals", "total": 3, "not_passed": 1}


def notice(run_id=RUN, findings=(FINDING,), aggregates=(), run_status="complete", **over):
    n = {
        "schema": 1,
        "kind": "finding_notice",
        "client": "trendlab",
        "run_id": run_id,
        "generated_at": "2026-10-12T05:40:00+00:00",
        "run": {
            "status": run_status,
            "kind": "suite",
            "started_at": "2026-10-12T05:30:00+00:00",
            "methodology_version": "1.2.0",
        },
        "counts": {"total": 12, "passed": 12 - len(findings), "by_split": {}},
        "findings": [copy.deepcopy(f) for f in findings],
        "aggregates": copy.deepcopy(list(aggregates)),
        "ops": {"run_status": run_status, "errored": 0, "ungraded": 0, "stop_reason": ""},
    }
    n.update(over)
    return n


def drop(outbox, n, name=None):
    folder = outbox / "trendlab"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name or n['run_id']}.json"
    path.write_text(n if isinstance(n, str) else json.dumps(n))
    return path


def ingest(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    return inbox, ingest_notices(inbox, tmp_path / "outbox", "trendlab")


def test_each_finding_becomes_one_open_item(tmp_path):
    drop(tmp_path / "outbox", notice())
    inbox, new = ingest(tmp_path)
    assert new == [RUN] and inbox.notice_row(RUN)["status"] == "ingested"
    [item] = inbox.list(status="open")
    assert item["source"] == "evals" and "tool-resourcefulness" in item["title"]
    assert item["evidence_refs"] == [f"evals:{RUN}:trendlab-capability-check"]
    assert item["card"] is None  # the evidence ref points at the notice; no card for raw findings


def test_the_ledger_keeps_the_validated_notice_for_the_report(tmp_path):
    drop(tmp_path / "outbox", notice())
    inbox, _ = ingest(tmp_path)
    [row] = inbox.pending_reports()
    assert row["run_id"] == RUN and json.loads(row["report_payload"]) == notice()


def test_a_notice_is_ingested_once(tmp_path):
    drop(tmp_path / "outbox", notice())
    inbox, _ = ingest(tmp_path)
    assert ingest_notices(inbox, tmp_path / "outbox", "trendlab") == []
    assert inbox.list(status="open")[0]["occurrences"] == 1


def test_a_recurring_failure_next_night_is_the_same_item(tmp_path):
    drop(tmp_path / "outbox", notice())
    drop(tmp_path / "outbox", notice(run_id="r-20261013T053000Z-bbbb"))
    inbox, new = ingest(tmp_path)
    assert len(new) == 2 and inbox.list(status="open")[0]["occurrences"] == 2


def test_aggregates_become_items_only_when_something_failed(tmp_path):
    drop(
        tmp_path / "outbox",
        notice(
            findings=(),
            aggregates=[
                {"split": "holdout", "capability": "safe-refusals", "total": 3, "not_passed": 1},
                {"split": "holdout", "capability": "config-knowledge", "total": 1, "not_passed": 0},
            ],
        ),
    )
    inbox, _ = ingest(tmp_path)
    [item] = inbox.list(status="open")
    assert (
        "holdout" in item["title"]
        and "safe-refusals" in item["title"]
        and "1 of 3" in item["title"]
    )


def test_canary_and_holdout_aggregates_never_merge(tmp_path):
    drop(
        tmp_path / "outbox",
        notice(
            findings=(),
            aggregates=[
                {"split": "holdout", "capability": "safe-refusals", "total": 3, "not_passed": 1},
                {"split": "canary", "capability": "safe-refusals", "total": 1, "not_passed": 1},
            ],
        ),
    )
    inbox, _ = ingest(tmp_path)
    items = inbox.list(status="open")
    assert len(items) == 2 and {i["title"].split(":")[0] for i in items} == {
        "Evals holdout",
        "Evals canary",
    }


def test_a_sealed_aborted_run_becomes_an_ops_item(tmp_path):
    drop(tmp_path / "outbox", notice(findings=(), run_status="aborted"))
    inbox, _ = ingest(tmp_path)
    [item] = inbox.list(status="open")
    assert "aborted" in item["title"] and item["severity"] == "high"


def test_a_recovered_run_is_accepted_as_an_ops_item(tmp_path):
    drop(tmp_path / "outbox", notice(findings=(), run_status="aborted_recovered"))
    inbox, new = ingest(tmp_path)
    [item] = inbox.list(status="open")
    assert new == [RUN] and "aborted_recovered" in item["title"]


def test_a_clean_run_writes_only_the_ledger_row(tmp_path):
    drop(tmp_path / "outbox", notice(findings=()))
    inbox, new = ingest(tmp_path)
    assert new == [RUN] and inbox.list(status=None) == []
    assert inbox.notice_row(RUN)["report_status"] == "pending"


def test_regression_checks_are_optional(tmp_path):
    no_checks = {k: v for k, v in FINDING.items() if k != "checks"}
    assert len(validate_notice(notice(findings=(no_checks,)), "trendlab")) == 1


def test_every_entry_carries_an_explicit_cluster_and_no_card():
    entries = validate_notice(
        notice(
            findings=(MAIN, FINDING),
            run_status="incomplete",
            aggregates=[{"split": "holdout", "capability": "x", "total": 2, "not_passed": 2}],
        ),
        "trendlab",
    )
    assert len(entries) == 4
    for e in entries:
        assert e["card"] is None and len(e["cluster"]) == 16
    assert len({e["cluster"] for e in entries}) == 4


BAD_NOTICES = {
    "missing finding field": lambda: notice(
        findings=({k: v for k, v in FINDING.items() if k != "capability"},)
    ),
    "wrong field type": lambda: notice(findings=({**FINDING, "failure_codes": "ROB.ROBUSTNESS"},)),
    "bad second finding": lambda: notice(findings=(MAIN, {**FINDING, "severity": 3})),
    "bad third finding": lambda: notice(findings=(MAIN, FINDING, {**FINDING, "split": "holdout"})),
    "client mismatch": lambda: notice(client="demo-helpdesk"),
    "bad aggregate": lambda: notice(
        aggregates=[{"split": "holdout", "capability": "x", "total": "3", "not_passed": 1}]
    ),
    "aggregate without split": lambda: notice(
        aggregates=[{"capability": "x", "total": 3, "not_passed": 1}]
    ),
    "aggregate not_passed > total": lambda: notice(
        aggregates=[{"split": "holdout", "capability": "x", "total": 1, "not_passed": 2}]
    ),
    "aggregate not an object": lambda: notice(aggregates=["holdout"]),
    "holdout instead of aggregates": lambda: {
        **{k: v for k, v in notice().items() if k != "aggregates"},
        "holdout": [],
    },
    "checks not a dict": lambda: notice(findings=({**FINDING, "checks": ["forecast"]},)),
    "checks non-string": lambda: notice(findings=({**FINDING, "checks": {"contains": [1]}},)),
    "checks unknown kind": lambda: notice(findings=({**FINDING, "checks": {"judge": ["x"]}},)),
    "checks too many": lambda: notice(findings=({**FINDING, "checks": {"contains": ["x"] * 21}},)),
    "checks too long": lambda: notice(findings=({**FINDING, "checks": {"regex": ["x" * 401]}},)),
    "checks on a main finding": lambda: notice(findings=({**MAIN, "checks": CHECKS},)),
    "missing counts": lambda: {k: v for k, v in notice().items() if k != "counts"},
    "ops not an object": lambda: notice(ops=[]),
    "newer schema": lambda: notice(schema=2),
    "unknown run status": lambda: notice(run_status="running"),
    "run id mismatch": lambda: notice(run_id="r-20261012T053000Z-zzzz"),
    "not an object": lambda: [notice()],
    # one row per guard (review finding 1)
    "wrong kind": lambda: notice(kind="x"),
    "passed > total": lambda: notice(counts={"total": 1, "passed": 2, "by_split": {}}),
    "by_split passed > total": lambda: notice(
        counts={"total": 12, "passed": 11, "by_split": {"main": {"total": 1, "passed": 2}}}
    ),
    "ops disagrees with run": lambda: notice(ops={**OPS, "run_status": "aborted"}),
    "finding not an object": lambda: notice(findings=("x",)),
    "bad case_id": lambda: notice(findings=({**FINDING, "case_id": "-x"},)),
    "passing status": lambda: notice(findings=({**FINDING, "status": "pass"},)),
    "long failure code": lambda: notice(findings=({**FINDING, "failure_codes": ["x" * 65]},)),
    "non-string failure code": lambda: notice(findings=({**FINDING, "failure_codes": [1]},)),
    "too many failure codes": lambda: notice(findings=({**FINDING, "failure_codes": ["X"] * 21},)),
    "gate int": lambda: notice(findings=({**FINDING, "gate": 3},)),
    "gate too long": lambda: notice(findings=({**FINDING, "gate": "g" * 65},)),
    "gate missing": lambda: notice(findings=({k: v for k, v in FINDING.items() if k != "gate"},)),
    "answer too long": lambda: notice(findings=({**FINDING, "answer_excerpt": "x" * 301},)),
    "judge reason too long": lambda: notice(findings=({**FINDING, "judge_reason": "x" * 201},)),
    "stop_reason too long": lambda: notice(ops={**OPS, "stop_reason": "x" * 121}),
    "main without expected_summary": lambda: notice(
        findings=({k: v for k, v in MAIN.items() if k != "expected_summary"},)
    ),
    "expected_summary too long": lambda: notice(
        findings=({**MAIN, "expected_summary": "x" * 201},)
    ),
    "regression without input": lambda: notice(
        findings=({k: v for k, v in FINDING.items() if k != "input"},)
    ),
    "input too long": lambda: notice(findings=({**FINDING, "input": "x" * 1001},)),
    "expected string too long": lambda: notice(findings=({**FINDING, "expected": ["x" * 401]},)),
    "too many expected": lambda: notice(findings=({**FINDING, "expected": ["x"] * 51},)),
    # unknown keys fail closed at every level (review finding 2)
    "unknown top-level key": lambda: notice(extra="x"),
    "unknown run key": lambda: notice(run={**notice()["run"], "ended_at": "x"}),
    "unknown counts key": lambda: notice(counts={**notice()["counts"], "cost_usd": 1}),
    "unknown by_split key": lambda: notice(
        counts={"total": 12, "passed": 11, "by_split": {"main": {"total": 1, "passed": 1, "x": 1}}}
    ),
    "unknown ops key": lambda: notice(ops={**OPS, "extra": 0}),
    "aggregate with case ids": lambda: notice(
        aggregates=[{**AGG, "case_ids": ["holdout-secret-case"]}]
    ),
    "main finding with expected": lambda: notice(findings=({**MAIN, "expected": ["criterion"]},)),
    "main finding with input": lambda: notice(findings=({**MAIN, "input": "x"},)),
    "unknown finding key": lambda: notice(findings=({**FINDING, "criteria": ["x"]},)),
    "unknown severity": lambda: notice(findings=({**FINDING, "severity": "critical"},)),
    "count is a bool": lambda: notice(ops={**OPS, "errored": True}),
    "negative count": lambda: notice(ops={**OPS, "ungraded": -1}),
    "malformed run id": lambda: notice(run_id="r-bad"),
    "long by_split name": lambda: notice(
        counts={"total": 12, "passed": 11, "by_split": {"s" * 65: {"total": 1, "passed": 1}}}
    ),
}


@pytest.mark.parametrize("case", sorted(BAD_NOTICES))
def test_a_malformed_notice_writes_nothing_but_one_rejected_item(tmp_path, case):
    drop(tmp_path / "outbox", json.dumps(BAD_NOTICES[case]()), name=RUN)
    inbox, new = ingest(tmp_path)
    assert new == [], case
    items = inbox.list(status=None)
    assert len(items) == 1 and "rejected" in items[0]["title"], case
    assert items[0]["source"] == "observe"
    row = inbox.notice_row(RUN)
    assert row["status"] == "rejected" and row["report_status"] is None, case
    assert inbox.pending_reports() == []


@pytest.mark.parametrize("case", sorted(set(BAD_NOTICES) - {"run id mismatch"}))
def test_each_bad_notice_fails_its_own_guard_not_a_fallback(case):
    with pytest.raises(NoticeError):  # not a TypeError/KeyError caught later by ingest_notices
        validate_notice(BAD_NOTICES[case](), "trendlab")


def test_validate_notice_never_touches_the_inbox_and_names_the_problem():
    with pytest.raises(NoticeError, match="findings\\[1\\].severity"):
        validate_notice(notice(findings=(MAIN, {**FINDING, "severity": 3})), "trendlab")


@pytest.mark.parametrize("error", [TypeError, KeyError, AttributeError])
def test_an_unexpected_validator_error_is_a_rejection_not_an_abort(tmp_path, monkeypatch, error):
    def odd(_notice, _client):
        raise error("odd")

    drop(tmp_path / "outbox", notice())
    drop(tmp_path / "outbox", notice(run_id="r-20261013T053000Z-bbbb"))
    monkeypatch.setattr(observe, "validate_notice", odd)
    inbox, new = ingest(tmp_path)
    assert new == [] and inbox.notice_row(RUN)["status"] == "rejected"
    assert inbox.notice_row("r-20261013T053000Z-bbbb")["status"] == "rejected"


def test_errors_on_a_complete_run_give_one_high_ops_item(tmp_path):
    drop(tmp_path / "outbox", notice(findings=(), ops={**OPS, "errored": 1}))
    inbox, new = ingest(tmp_path)
    [item] = inbox.list(status="open")
    assert new == [RUN] and item["severity"] == "high" and "errored 1" in item["title"]


def test_the_stored_payload_is_rebuilt_from_validated_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(observe, "validate_notice", lambda _n, _c: [])  # let an extra key through
    drop(tmp_path / "outbox", notice(extra="SMUGGLED", aggregates=[{**AGG, "case_ids": ["s"]}]))
    inbox, _ = ingest(tmp_path)
    payload = inbox.notice_row(RUN)["report_payload"]
    assert "SMUGGLED" not in payload and "case_ids" not in payload
    assert json.loads(payload) == notice(aggregates=[AGG])


# Produced by trendlab-evals `worker_notice` (il-a @ 86f4e2d, its test fixture `make_run`).
EMITTED = {
    "schema": 1,
    "kind": "finding_notice",
    "client": "trendlab",
    "run_id": "r-20261012T053000Z-a1b2",
    "generated_at": "2026-10-12T04:21:47+00:00",
    "run": {
        "status": "complete",
        "kind": "suite",
        "started_at": "2026-10-12T05:30:00+00:00",
        "methodology_version": "1.2.0",
    },
    "counts": {
        "total": 5,
        "passed": 2,
        "by_split": {
            "holdout": {"total": 2, "passed": 1},
            "main": {"total": 2, "passed": 1},
            "regression": {"total": 1, "passed": 0},
        },
    },
    "findings": [
        {
            "case_id": "main-bad",
            "split": "main",
            "status": "fail",
            "capability": "tool-resourcefulness",
            "category": "accuracy",
            "severity": "high",
            "gate": None,
            "failure_codes": ["ACC.FACTUAL"],
            "answer_excerpt": "I cannot browse. [REDACTED]",
            "judge_reason": "did not try the web tool",
            "expected_summary": "RATIONALE-SUMMARY " * 11 + "R\u2026",
        },
        {
            "case_id": "reg-bad",
            "split": "regression",
            "status": "fail",
            "capability": "tool-resourcefulness",
            "category": "accuracy",
            "severity": "med",
            "gate": None,
            "failure_codes": ["ACC.FACTUAL"],
            "answer_excerpt": "no web",
            "judge_reason": "bad",
            "input": "REGRESSION-INPUT",
            "expected": ["REGRESSION-CRITERION"],
            "checks": {"contains": [], "not_contains": [], "regex": []},
        },
    ],
    "aggregates": [
        {"split": "holdout", "capability": "safe-refusals", "total": 2, "not_passed": 1}
    ],
    "ops": {"run_status": "complete", "errored": 0, "ungraded": 0, "stop_reason": ""},
}


def test_a_notice_from_the_real_emitter_round_trips(tmp_path):
    assert len(EMITTED["findings"][0]["expected_summary"]) == 200  # the emitter's cap
    drop(tmp_path / "outbox", EMITTED)
    inbox, new = ingest(tmp_path)
    assert new == [EMITTED["run_id"]] and len(inbox.list(status="open")) == 3
    assert json.loads(inbox.notice_row(EMITTED["run_id"])["report_payload"]) == EMITTED


def test_emitter_nulls_in_run_metadata_are_accepted():
    run = {"status": "complete", "kind": None, "started_at": None, "methodology_version": None}
    assert validate_notice({**EMITTED, "run": run}, "trendlab")


def test_an_oversized_notice_is_rejected_once_without_a_full_read(tmp_path):
    drop(tmp_path / "outbox", " " * (observe.MAX_NOTICE_BYTES + 10), name=RUN)
    inbox, _ = ingest(tmp_path)
    ingest_notices(inbox, tmp_path / "outbox", "trendlab")
    [item] = inbox.list(status="open")
    assert item["occurrences"] == 1 and "too large" in inbox.notice_row(RUN)["detail"]


def test_unparseable_json_is_rejected_once_not_every_cycle(tmp_path):
    drop(tmp_path / "outbox", "{broken", name=RUN)
    inbox, _ = ingest(tmp_path)
    ingest_notices(inbox, tmp_path / "outbox", "trendlab")
    [item] = inbox.list(status="open")
    assert item["occurrences"] == 1


def test_invalid_utf8_is_rejected(tmp_path):
    folder = tmp_path / "outbox" / "trendlab"
    folder.mkdir(parents=True)
    (folder / f"{RUN}.json").write_bytes(b"\xff\xfe{")
    inbox, _ = ingest(tmp_path)
    assert inbox.notice_row(RUN)["status"] == "rejected"


def test_a_rejected_notice_that_is_rewritten_valid_is_then_ingested(tmp_path):
    drop(tmp_path / "outbox", "{broken", name=RUN)
    inbox, _ = ingest(tmp_path)
    drop(tmp_path / "outbox", notice())
    assert ingest_notices(inbox, tmp_path / "outbox", "trendlab") == [RUN]
    assert inbox.notice_row(RUN)["status"] == "ingested"


def test_files_that_are_not_run_ids_are_ignored(tmp_path):
    drop(tmp_path / "outbox", notice(), name="r-notarunid")
    inbox, new = ingest(tmp_path)
    assert new == [] and inbox.list(status=None) == [] and inbox.notice_row("r-notarunid") is None


def test_a_database_error_mid_batch_leaves_no_partial_items(tmp_path, monkeypatch):
    drop(tmp_path / "outbox", notice(findings=(MAIN, FINDING)))
    inbox = Inbox(tmp_path / "inbox.db")
    real = inbox._record_locked
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full")
        return real(**kw)

    monkeypatch.setattr(inbox, "_record_locked", flaky)
    with pytest.raises(RuntimeError):
        ingest_notices(inbox, tmp_path / "outbox", "trendlab")
    assert inbox.list(status=None) == [] and inbox.notice_row(RUN) is None


def test_a_failed_second_notice_is_retried_alone_next_cycle(tmp_path, monkeypatch):
    second = "r-20261013T053000Z-bbbb"
    drop(tmp_path / "outbox", notice(findings=(MAIN,)))
    drop(tmp_path / "outbox", notice(run_id=second, findings=(FINDING,)))
    inbox = Inbox(tmp_path / "inbox.db")
    real = inbox._record_locked
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full")
        return real(**kw)

    monkeypatch.setattr(inbox, "_record_locked", flaky)
    with pytest.raises(RuntimeError):
        ingest_notices(inbox, tmp_path / "outbox", "trendlab")
    monkeypatch.undo()
    assert ingest_notices(inbox, tmp_path / "outbox", "trendlab") == [second]
    assert [i["occurrences"] for i in inbox.list(status="open")] == [1, 1]


def test_missing_outbox_is_a_quiet_no_op(tmp_path):
    assert ingest(tmp_path)[1] == []


def test_sixty_one_notices_are_each_ingested_exactly_once_forever(tmp_path):
    from datetime import date, timedelta

    for day in range(61):
        d = date(2026, 10, 12) + timedelta(days=day)
        drop(tmp_path / "outbox", notice(run_id=f"r-{d:%Y%m%d}T053000Z-a{day:03d}", findings=()))
    inbox = Inbox(tmp_path / "inbox.db")
    assert len(ingest_notices(inbox, tmp_path / "outbox", "trendlab")) == 61
    for _ in range(3):
        assert ingest_notices(inbox, tmp_path / "outbox", "trendlab") == []


def test_restart_between_commit_and_anything_else_does_not_reingest(tmp_path):
    drop(tmp_path / "outbox", notice())
    first = Inbox(tmp_path / "inbox.db")
    ingest_notices(first, tmp_path / "outbox", "trendlab")
    first.close()  # simulated crash: no state.json involved at all
    second = Inbox(tmp_path / "inbox.db")
    assert ingest_notices(second, tmp_path / "outbox", "trendlab") == []
    assert second.list(status="open")[0]["occurrences"] == 1


def test_findings_differing_only_in_digits_stay_separate(tmp_path):
    day30 = {**MAIN, "case_id": "return-window-day-30"}
    day31 = {**MAIN, "case_id": "return-window-day-31"}
    drop(tmp_path / "outbox", notice(findings=(day30, day31)))
    inbox = Inbox(tmp_path / "inbox.db")
    ingest_notices(inbox, tmp_path / "outbox", "trendlab")
    assert len(inbox.list(status="open")) == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode 000 files")
def test_an_unreadable_notice_is_escalated_after_three_cycles(tmp_path):
    path = drop(tmp_path / "outbox", notice())
    path.chmod(0o000)
    try:
        inbox = Inbox(tmp_path / "inbox.db")
        for _ in range(2):
            ingest_notices(inbox, tmp_path / "outbox", "trendlab")
        assert inbox.list(status="open") == []
        ingest_notices(inbox, tmp_path / "outbox", "trendlab")
        [item] = inbox.list(status="open")
        assert "unreadable" in item["title"] and item["source"] == "observe"
        assert inbox.notice_summary("trendlab")["unreadable_notices"] == [RUN]
    finally:
        path.chmod(0o644)  # restore so pytest can clean tmp_path
    assert ingest_notices(inbox, tmp_path / "outbox", "trendlab") == [RUN]
    assert inbox.notice_summary("trendlab")["unreadable_notices"] == []


def test_deeply_nested_json_is_rejected_once(tmp_path):
    drop(tmp_path / "outbox", "[" * 100000 + "]" * 100000, name=RUN)
    inbox = Inbox(tmp_path / "inbox.db")
    ingest_notices(inbox, tmp_path / "outbox", "trendlab")
    ingest_notices(inbox, tmp_path / "outbox", "trendlab")
    [item] = inbox.list(status="open")
    assert item["occurrences"] == 1 and inbox.notice_row(RUN)["status"] == "rejected"


@pytest.mark.parametrize(
    ("bad", "message"),
    [([notice()], "notice is not an object"), (notice(findings=("x",)), r"findings\[0\] is not")],
)
def test_non_objects_are_named_as_such(bad, message):
    with pytest.raises(NoticeError, match=message):
        validate_notice(bad, "trendlab")


# -- Observer report, retryable delivery, status file (Task 4) ---------------------------------


def test_the_report_quotes_only_recorded_numbers():
    n = notice(counts={"total": 12, "passed": 11, "by_split": {"main": {"total": 8, "passed": 8},
                                                               "regression": {"total": 4, "passed": 3}}})  # fmt: skip
    text = observer_report(n, open_items=5)
    assert text.splitlines()[0] == f"🔎 TrendLab Observer · run {RUN} (complete)"
    assert "Suite: 11/12 passed (main 8/8 · regression 3/4)" in text
    assert "Open items: 5" in text
    assert "• regression fail: tool-resourcefulness — trendlab-capability-check" in text
    assert text.endswith("Observer only (L0): recorded, nothing was changed.")
    assert "I cannot browse" not in text and "no lookup" not in text  # no finding content


def test_a_clean_night_says_so():
    text = observer_report(notice(findings=()), open_items=0)
    assert "Suite: 12/12 passed" in text and "Open items: 0" in text
    assert text.endswith("No new failures. Observer only (L0): nothing was changed.")


def test_aggregates_name_their_split():
    text = observer_report(notice(findings=(), aggregates=[AGG]), open_items=1)
    assert "• holdout: safe-refusals 1 of 3 not passed" in text
    assert text.endswith("recorded, nothing was changed.")


def test_a_failed_send_is_retried_next_cycle_without_reingesting(tmp_path):
    drop(tmp_path / "outbox", notice())
    inbox, _ = ingest(tmp_path)
    sent, results = [], iter([False, True])

    async def send(text):
        sent.append(text)
        return next(results)

    first = asyncio.run(deliver_reports(inbox, send))
    assert first == {"reported": [], "abandoned": []}
    assert inbox.notice_row(RUN)["report_status"] == "pending"
    assert ingest_notices(inbox, tmp_path / "outbox", "trendlab") == []
    assert inbox.list(status="open")[0]["occurrences"] == 1
    assert asyncio.run(deliver_reports(inbox, send)) == {"reported": [RUN], "abandoned": []}
    assert inbox.notice_row(RUN)["report_status"] == "sent"
    assert asyncio.run(deliver_reports(inbox, send)) == {"reported": [], "abandoned": []}
    assert len(sent) == 2


def test_a_send_exception_is_a_failed_attempt(tmp_path):
    drop(tmp_path / "outbox", notice())
    inbox, _ = ingest(tmp_path)

    async def send(text):
        raise RuntimeError("network down")

    assert asyncio.run(deliver_reports(inbox, send)) == {"reported": [], "abandoned": []}
    row = inbox.notice_row(RUN)
    assert row["report_status"] == "pending" and row["report_attempts"] == 1


def test_a_report_is_abandoned_at_the_bound_with_one_escalation(tmp_path):
    drop(tmp_path / "outbox", notice())
    inbox, _ = ingest(tmp_path)

    async def send(text):
        return False

    for _ in range(2):
        assert asyncio.run(deliver_reports(inbox, send, max_attempts=3))["abandoned"] == []
    assert asyncio.run(deliver_reports(inbox, send, max_attempts=3))["abandoned"] == [RUN]
    assert asyncio.run(deliver_reports(inbox, send, max_attempts=3))["abandoned"] == []
    assert inbox.notice_row(RUN)["report_status"] == "abandoned"
    escalations = [i for i in inbox.list(status="open") if "undeliverable" in i["title"]]
    assert len(escalations) == 1 and escalations[0]["occurrences"] == 1


def test_write_status_posts_the_ledger_atomically_with_mode_640(tmp_path):
    drop(tmp_path / "outbox", notice())
    inbox, _ = ingest(tmp_path)
    folder = tmp_path / "exchange"
    folder.mkdir()
    ack = folder / "observe-status.json"
    assert write_status(inbox, "trendlab", ack) is True
    data = json.loads(ack.read_text())
    assert data["schema"] == 1 and data["client"] == "trendlab"
    assert data["last_observed_run"] == RUN and data["pending_reports"] == [RUN]
    assert data["abandoned_reports"] == [] and data["rejected_notices"] == []
    assert data["unreadable_notices"] == [] and data["updated_at"].endswith("+00:00")
    assert os.stat(ack).st_mode & 0o777 == 0o640
    assert sorted(p.name for p in folder.iterdir()) == ["observe-status.json"]  # no temp left


def test_write_status_returns_false_on_an_unwritable_folder(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    assert write_status(inbox, "trendlab", tmp_path / "missing" / "observe-status.json") is False
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        if not os.access(locked, os.W_OK):  # root can write anywhere
            assert write_status(inbox, "trendlab", locked / "observe-status.json") is False
    finally:
        locked.chmod(0o700)


def test_a_poison_report_row_is_a_failed_attempt_and_is_abandoned(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    bad, good = "r-20261011T053000Z-aaaa", RUN
    inbox.ingest_notice(run_id=bad, client="trendlab", fingerprint="f", entries=[],
                        payload=json.dumps({"no": "run id"}))  # fmt: skip
    drop(tmp_path / "outbox", notice())
    ingest_notices(inbox, tmp_path / "outbox", "trendlab")
    sent = []

    async def send(text):
        sent.append(text)
        return True

    first = asyncio.run(deliver_reports(inbox, send, max_attempts=2))
    assert first == {"reported": [good], "abandoned": []}  # the bad row blocks nothing behind it
    assert inbox.notice_row(bad)["report_attempts"] == 1
    assert asyncio.run(deliver_reports(inbox, send, max_attempts=2))["abandoned"] == [bad]
    assert len(sent) == 1


def test_write_status_never_raises_on_a_database_error(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    inbox.close()  # any query now raises sqlite3.ProgrammingError
    ack = tmp_path / "observe-status.json"
    assert write_status(inbox, "trendlab", ack) is False
    assert sorted(p.name for p in tmp_path.iterdir()) == ["inbox.db"]


def test_write_status_reports_outbox_readability(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    ack = tmp_path / "observe-status.json"
    assert write_status(inbox, "trendlab", ack, outbox_readable=False) is True
    assert json.loads(ack.read_text())["outbox_readable"] is False


def test_an_unreadable_outbox_raises_instead_of_looking_empty(tmp_path):
    drop(tmp_path / "outbox", notice())
    folder = tmp_path / "outbox" / "trendlab"
    folder.chmod(0o300)  # traversable, not listable
    try:
        if os.access(folder, os.R_OK):
            pytest.skip("running as root")
        with pytest.raises(PermissionError):
            ingest_notices(Inbox(tmp_path / "inbox.db"), tmp_path / "outbox", "trendlab")
    finally:
        folder.chmod(0o700)
