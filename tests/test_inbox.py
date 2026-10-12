"""Inbox writes: record is unchanged for callers; the notice ledger is one transaction per action."""

import pytest

from trendlab.engine.inbox import Inbox

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
