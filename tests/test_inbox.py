"""Inbox writes: record is unchanged for callers; the notice ledger is one transaction per action."""

import hashlib
import json
import sqlite3

import pytest

from trendlab.engine.inbox import Inbox, split_signature

RUN = "r-20261012T053000Z-aaaa"


def entry(n, **over):
    e = {
        "project": "harness:trendlab-cli",
        "title": f"Evals main fail: cap-{n}",
        "signature": f"evals|main|cap-{n}",
        "source": "evals",
        "evidence": [f"evals:{RUN}:case-{n}"],
        "severity": "med",
        "card": None,
        "cluster": f"cluster{n:09d}",
    }
    e.update(over)
    return e


def test_record_merges_reopens_and_keeps_silenced(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    a = inbox.record(project="/p", title="boom", signature="Error at line 10", evidence=["s1"])
    assert a["occurrences"] == 1 and a["status"] == "open" and a["card"] is None
    b = inbox.record(project="/p", title="boom", signature="Error at line 99", evidence=["s2"])
    assert b["id"] == a["id"] and b["occurrences"] == 2 and b["evidence_refs"] == ["s1", "s2"]
    inbox.set_status(a["id"], "fixed")
    c = inbox.record(project="/p", title="boom", signature="Error at line 1")
    assert c["id"] == a["id"] and c["status"] == "open" and c["occurrences"] == 3
    inbox.set_status(a["id"], "silenced")
    d = inbox.record(project="/p", title="boom", signature="Error at line 2")
    assert d["status"] == "silenced" and d["occurrences"] == 4
    inbox.close()


def test_record_caps_evidence_and_unions_impacted_files(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    for i in range(25):
        last = inbox.record(
            project="/p",
            title="t",
            signature="sig",
            evidence=[f"e{i}"],
            impacted_files=[f"f{i % 3}"],
        )
    assert last["evidence_refs"] == [f"e{i}" for i in range(5, 25)]
    assert last["impacted_files"] == ["f0", "f1", "f2"]


def test_record_with_card_stores_it_and_a_later_merge_keeps_it(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    a = inbox.record(project="/p", title="t", signature="sig", card={"problem": "p"})
    assert a["card"] == {"problem": "p"}
    b = inbox.record(project="/p", title="t", signature="sig")
    assert b["card"] == {"problem": "p"}
    c = inbox.record(project="/p", title="t", signature="sig", card={"problem": "q"})
    assert c["card"] == {"problem": "q"}


def test_ingest_notice_writes_entries_and_ledger_row_together(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    inbox.ingest_notice(
        run_id=RUN, client="trendlab", fingerprint="f1", entries=[entry(1), entry(2)], payload="{}"
    )
    assert inbox.counts() == {"open": 2}
    row = inbox.notice_row(RUN)
    assert row["status"] == "ingested" and row["report_status"] == "pending"
    assert row["fingerprint"] == "f1" and row["report_payload"] == "{}"
    assert [r["run_id"] for r in inbox.pending_reports()] == [RUN]


def test_explicit_cluster_bypasses_digit_normalization(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    a = entry(1, signature="evals|main|cap|day-30", cluster="aaaaaaaaaaaaaaaa")
    b = entry(1, signature="evals|main|cap|day-31", cluster="bbbbbbbbbbbbbbbb")
    inbox.ingest_notice(
        run_id=RUN, client="trendlab", fingerprint="f", entries=[a, b], payload="{}"
    )
    assert inbox.counts() == {"open": 2}


def test_ingest_notice_failure_on_second_entry_leaves_nothing(tmp_path, monkeypatch):
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
        inbox.ingest_notice(
            run_id=RUN,
            client="trendlab",
            fingerprint="f",
            entries=[entry(1), entry(2)],
            payload="{}",
        )
    assert inbox.counts() == {} and inbox.list(status=None) == []
    assert inbox.notice_row(RUN) is None
    monkeypatch.undo()
    inbox.ingest_notice(
        run_id=RUN, client="trendlab", fingerprint="f", entries=[entry(1)], payload="{}"
    )
    assert inbox.counts() == {"open": 1}  # the connection is usable after the rollback


def test_ingest_notice_never_ingests_twice(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    kw = {"run_id": RUN, "client": "trendlab", "fingerprint": "f", "payload": "{}"}
    assert inbox.ingest_notice(entries=[entry(1)], **kw) is True
    inbox.mark_report(RUN, sent=True, max_attempts=3)
    assert inbox.ingest_notice(entries=[entry(1)], **kw) is False
    assert inbox.list()[0]["occurrences"] == 1 and inbox.notice_row(RUN)["report_status"] == "sent"


@pytest.mark.parametrize(
    "action",
    [
        lambda ib: ib.reject_notice(run_id=RUN, client="trendlab", fingerprint="g", detail="d"),
        lambda ib: ib.note_unreadable(run_id=RUN, client="trendlab", threshold=1),
        lambda ib: ib.mark_report(RUN, sent=False, max_attempts=1),
    ],
    ids=["reject_notice", "note_unreadable", "mark_report"],
)
def test_ledger_writes_roll_back_on_error(tmp_path, monkeypatch, action):
    inbox = Inbox(tmp_path / "inbox.db")
    inbox.ingest_notice(run_id=RUN, client="trendlab", fingerprint="f", entries=[], payload="{}")
    before = inbox.notice_row(RUN)

    def boom(**_kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(inbox, "_record_locked", boom)
    with pytest.raises(RuntimeError):
        action(inbox)
    assert inbox.notice_row(RUN) == before and inbox.list(status=None) == []
    monkeypatch.undo()
    assert inbox.record(project="/p", title="t", signature="s")["occurrences"] == 1


def test_reject_notice_records_one_item_and_a_rejected_row(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    inbox.reject_notice(run_id=RUN, client="trendlab", fingerprint="f", detail="x" * 500)
    [item] = inbox.list()
    assert "rejected" in item["title"] and item["source"] == "observe"
    assert item["evidence_refs"] == [f"evals:{RUN}:notice"]
    row = inbox.notice_row(RUN)
    assert row["status"] == "rejected" and row["fingerprint"] == "f" and len(row["detail"]) == 200
    assert row["report_status"] is None
    assert inbox.notice_summary("trendlab")["rejected_notices"] == [RUN]


def test_note_unreadable_escalates_on_the_third_failure_only(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    assert inbox.note_unreadable(run_id=RUN, client="trendlab") is False
    assert inbox.note_unreadable(run_id=RUN, client="trendlab") is False
    assert inbox.list() == []
    assert inbox.notice_summary("trendlab")["unreadable_notices"] == []
    assert inbox.note_unreadable(run_id=RUN, client="trendlab") is True
    [item] = inbox.list()
    assert "unreadable" in item["title"] and item["source"] == "observe"
    assert inbox.notice_row(RUN)["read_failures"] == 3
    assert inbox.notice_summary("trendlab")["unreadable_notices"] == [RUN]
    assert inbox.note_unreadable(run_id=RUN, client="trendlab") is False
    assert inbox.list()[0]["occurrences"] == 1


def test_mark_report_sent_pending_and_abandoned(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    for run in ("r-20261012T053000Z-aaaa", "r-20261013T053000Z-bbbb"):
        inbox.ingest_notice(
            run_id=run, client="trendlab", fingerprint="f", entries=[], payload="{}"
        )
    assert inbox.mark_report("r-20261012T053000Z-aaaa", sent=True, max_attempts=3) == "sent"
    b = "r-20261013T053000Z-bbbb"
    assert inbox.mark_report(b, sent=False, max_attempts=3) == "pending"
    assert inbox.mark_report(b, sent=False, max_attempts=3) == "pending"
    assert inbox.notice_row(b)["report_attempts"] == 2 and inbox.list() == []
    assert inbox.mark_report(b, sent=False, max_attempts=3) == "abandoned"
    [item] = inbox.list()
    assert "undeliverable" in item["title"] and item["source"] == "observe"
    assert inbox.mark_report(b, sent=False, max_attempts=3) == "abandoned"
    assert len(inbox.list()) == 1 and inbox.list()[0]["occurrences"] == 1
    assert inbox.pending_reports() == []
    summary = inbox.notice_summary("trendlab")
    assert summary["last_observed_run"] == b
    assert summary["pending_reports"] == [] and summary["abandoned_reports"] == [b]


def test_mark_report_unknown_run_raises(tmp_path):
    with pytest.raises(KeyError):
        Inbox(tmp_path / "inbox.db").mark_report(RUN, sent=True, max_attempts=3)


def test_notice_summary_is_per_client_and_capped(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    runs = [f"r-20261012T0530{i:02d}Z-aaaa" for i in range(25)]
    for run in runs:
        inbox.ingest_notice(
            run_id=run, client="trendlab", fingerprint="f", entries=[], payload="{}"
        )
    inbox.ingest_notice(
        run_id="r-20261101T053000Z-zzzz", client="other", fingerprint="f", entries=[], payload="{}"
    )
    summary = inbox.notice_summary("trendlab")
    assert summary["last_observed_run"] == runs[-1]
    assert summary["pending_reports"] == runs[-20:]
    assert summary["unreadable_notices"] == [] and summary["rejected_notices"] == []
    assert inbox.notice_summary("nobody")["last_observed_run"] is None


# -- IL-B records (evals spec §30.3, §30.5, D-110) -------------------------------------------------


OLD_SCHEMA = """
CREATE TABLE issue (
    id TEXT PRIMARY KEY, project TEXT NOT NULL, cluster_key TEXT NOT NULL, title TEXT NOT NULL,
    root_cause TEXT, impacted_files TEXT NOT NULL DEFAULT '[]',
    evidence_refs TEXT NOT NULL DEFAULT '[]', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1, severity TEXT NOT NULL DEFAULT 'med',
    status TEXT NOT NULL DEFAULT 'open', source TEXT NOT NULL DEFAULT 'run',
    proposed_patch_ref TEXT, verification_ref TEXT, feedback TEXT, card TEXT
);
CREATE TABLE notice (
    run_id TEXT PRIMARY KEY, client TEXT NOT NULL, status TEXT NOT NULL, fingerprint TEXT,
    detail TEXT, read_failures INTEGER NOT NULL DEFAULT 0, report_status TEXT,
    report_payload TEXT, report_attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
"""


def _old_notice():
    return {
        "schema": 1,
        "kind": "finding_notice",
        "client": "trendlab",
        "run_id": RUN,
        "generated_at": "2026-10-12T05:40:00+00:00",
        "run": {"status": "complete", "kind": "suite", "started_at": None,
                "methodology_version": None},
        "counts": {"total": 2, "passed": 1, "by_split": {}},
        "findings": [{
            "case_id": "case-7", "split": "main", "status": "fail", "capability": "a|b",
            "category": "x", "severity": "high", "gate": None, "failure_codes": ["E.ONE"],
            "answer_excerpt": "", "judge_reason": "", "expected_summary": "",
        }],
        "aggregates": [],
        "ops": {"run_status": "complete", "errored": 0, "ungraded": 0, "stop_reason": ""},
    }  # fmt: skip


def test_old_schema_migrates_keeps_rows_and_backfills_signatures(tmp_path):
    path = tmp_path / "inbox.db"
    sig = "evals|main|a|b|case-7|E.ONE"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO issue(id, project, cluster_key, title, first_seen, last_seen, source) "
        "VALUES('i1','harness:trendlab-cli',?,'t','2026-10-12','2026-10-12','evals')",
        (hashlib.sha1(sig.encode()).hexdigest()[:16],),
    )
    conn.execute(
        "INSERT INTO issue(id, project, cluster_key, title, first_seen, last_seen, source) "
        "VALUES('i2','harness:trendlab-cli','nomatch','t2','2026-10-12','2026-10-12','evals')"
    )
    conn.execute(
        "INSERT INTO notice(run_id, client, status, report_payload, updated_at) "
        "VALUES(?, 'trendlab', 'ingested', ?, '2026-10-12')",
        (RUN, json.dumps(_old_notice())),
    )
    conn.execute(
        "INSERT INTO notice(run_id, client, status, report_payload, updated_at) "
        "VALUES('r-20261011T053000Z-bbbb', 'trendlab', 'ingested', '{bad json', '2026-10-12')"
    )
    conn.commit()
    conn.close()
    inbox = Inbox(path)
    assert inbox.get("i1")["signature"] == sig
    assert inbox.get("i2")["signature"] is None  # no notice names it: stays NULL, ineligible
    Inbox(path).close()  # a second open finds the column and does not fail
    inbox.close()


def test_record_stores_the_signature_once(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    a = inbox.record(project="h", title="t", signature="evals|main|c|k|E", source="evals")
    assert a["signature"] == "evals|main|c|k|E"
    run = inbox.record(project="/p", title="boom", signature="Traceback secret", source="run")
    assert run["signature"] is None  # raw run text is never stored, only evals/observe keys
    inbox.close()


def test_split_signature_parses_from_the_right():
    assert split_signature("evals|main|cap|case-1|E.A+E.B") == (
        "evals|main|cap",
        "case-1",
        "E.A+E.B",
    )
    assert split_signature("evals|regression|a|b|c-2|none") == (
        "evals|regression|a|b",
        "c-2",
        "none",
    )
    assert split_signature("evals|main|cap") is None  # aggregate (3 parts)
    assert split_signature("evals|main|cap|case") is None  # 4 parts
    assert split_signature("evals|ops|complete|x|y") is None
    assert split_signature("evals|holdout|cap|c|E") is None
    # a code containing "|": the case id from the evidence ref anchors the cut
    assert split_signature("evals|main|cap|c-1|E|X", case_id="c-1") == (
        "evals|main|cap",
        "c-1",
        "E|X",
    )
    assert split_signature("evals|main|cap|c-1|E|X", case_id="zz") is None
    # ambiguous: the case id appears twice
    assert split_signature("evals|main|c-1|c-1|c-1|E", case_id="c-1") is None


def _start(inbox, item="abc123", prefix="evals|main|cap|case-1", day="2026-10-12", usd=0.5):
    return inbox.start_candidate(
        item_id=item, case_prefix=prefix, day=day, base_sha="a" * 40, reserved_usd=usd,
        worktree=f"/wt/{item}", branch="improve/x",
    )  # fmt: skip


def test_candidate_lifecycle_and_one_drafting_at_a_time(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    c1 = _start(inbox)
    assert c1["id"] == "cand-abc123-1" and c1["status"] == "drafting" and c1["attempt"] == 1
    with pytest.raises(RuntimeError, match="drafting"):
        _start(inbox)
    inbox.set_candidate_session(c1["id"], "sess-1")
    assert inbox.drafting_rows()[0]["session_id"] == "sess-1"
    inbox.finish_candidate(c1["id"], status="tests_failed", spend_usd=0.2, tests='{"passed": 1}')
    assert inbox.drafting_rows() == []
    c2 = _start(inbox, usd=0.3)
    assert c2["id"] == "cand-abc123-2" and inbox.prefix_attempts("evals|main|cap|case-1") == 2
    # the reservation counts until the true-up replaces it
    assert inbox.prefix_spend("evals|main|cap|case-1") == pytest.approx(0.5)
    assert inbox.spent_today("2026-10-12") == pytest.approx(0.5)
    assert inbox.spent_month("2026-10") == pytest.approx(0.5)
    inbox.finish_candidate(c2["id"], status="handed_off", spend_usd=0.1, head_sha="b" * 40)
    assert inbox.prefix_spend("evals|main|cap|case-1") == pytest.approx(0.3)
    assert inbox.prefix_has_handoff("evals|main|cap|case-1")
    assert not inbox.prefix_has_handoff("evals|main|cap|case-2")
    assert inbox.pending_handoffs() == 1
    assert inbox.pending_handoffs(consumed={"cand-abc123-2"}) == 0
    assert inbox.last_attempt_at("evals|main|cap|case-1") == c2["started_at"]
    assert inbox.last_attempt_at("nope") is None
    assert inbox.spent_today("2026-10-13") == 0
    assert inbox.spent_month("2026-11") == 0
    assert inbox.candidate(c2["id"])["head_sha"] == "b" * 40
    with pytest.raises(ValueError):
        inbox.finish_candidate(c2["id"], status="bogus", spend_usd=0)
    with pytest.raises(ValueError):
        inbox.finish_candidate(c2["id"], status="error", spend_usd=0, evil="x")
    inbox.close()


def test_candidate_writes_roll_back_on_error(tmp_path, monkeypatch):
    inbox = Inbox(tmp_path / "inbox.db")
    c1 = _start(inbox)
    with pytest.raises(sqlite3.Error):
        inbox.finish_candidate(c1["id"], status="error", spend_usd=0.1, files_changed=object())
    assert inbox.candidate(c1["id"])["status"] == "drafting"
    assert inbox.spent_today("2026-10-12") == pytest.approx(0.5)
    inbox.close()


def test_lessons_for_matches_split_and_capability(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    rows = [
        ("l1", "evals|main|cap|other-case|E", 1, 0, "active"),
        ("l2", "evals|main|cap|x|E", 5, 1, "active"),
        ("l3", "evals|main|cap|y|E", 9, 0, "retired"),
        ("l4", "evals|main|capx|y|E", 9, 0, "active"),
        ("l5", "evals|regression|cap|y|E", 9, 0, "active"),
    ]
    with inbox._lock:
        for lid, sig, ok, bad, status in rows:
            inbox._conn.execute(
                "INSERT INTO lesson(id, signature, symptom, fix_pattern, evidence, succeeded, "
                "failed, status, created_at, updated_at) VALUES(?,?,'s','f','e',?,?,?,'t','t')",
                (lid, sig, ok, bad, status),
            )
        inbox._conn.commit()
    got = [r["id"] for r in inbox.lessons_for("evals|main|cap|case-1")]
    assert got == ["l2", "l1"]
    assert len(inbox.lessons_for("evals|main|cap|case-1", limit=1)) == 1
    inbox.close()


def test_improvement_config_pins_the_il_b_caps():
    from trendlab.config.schema import ImprovementConfig

    imp = ImprovementConfig()
    assert (imp.propose, imp.scheduled) == (False, False)
    assert (imp.max_case_usd, imp.max_day_usd, imp.max_month_usd) == (0.5, 0.5, 10.0)
    assert (imp.max_attempts_per_case, imp.max_draft_minutes, imp.cooldown_days) == (3, 60, 7)
    assert (imp.max_pending_handoffs, imp.recent_days, imp.prune_after_days) == (10, 7, 14)
