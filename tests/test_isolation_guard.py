# tests/test_isolation_guard.py
"""IL-0 worker guard (evals spec §30.2, D-094, D-099): unattended candidate work needs a fresh,
referee-written proof of the boundary."""

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from trendlab.engine.isolation import RECORD, check_isolation

WORKER, REFEREE = 2001, 1000
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def write_record(tmp_path, **overrides):
    record = {
        "ok": True,
        "worker_uid": WORKER,
        "referee_uid": REFEREE,
        "static_checked_at": (NOW - timedelta(hours=3)).isoformat(),
        "live": {
            "ok": True,
            "checked_at": (NOW - timedelta(hours=11)).isoformat(),
            "source": "root-probe",
        },
        "protected_paths": [str(tmp_path / "referee-home")],
    }
    record.update(overrides)
    path = tmp_path / RECORD
    path.write_text(json.dumps(record))
    os.chmod(path, 0o644)
    return path


def check(tmp_path, **kw):
    kw.setdefault("uid", WORKER)
    kw.setdefault("gids", [WORKER])
    kw.setdefault("now", NOW.timestamp())
    kw.setdefault("sudo_works", lambda: False)
    kw.setdefault("can_read", lambda p: False)
    kw.setdefault("owner_of", lambda p: REFEREE)
    return check_isolation(tmp_path, **kw)


def test_a_fresh_verified_boundary_passes(tmp_path):
    write_record(tmp_path)
    status = check(tmp_path)
    assert status.ok, status.reasons


def test_missing_record_fails_closed(tmp_path):
    status = check(tmp_path)
    assert not status.ok and "no readable referee isolation record" in status.reasons[0]


def test_malformed_record_fails_closed(tmp_path):
    (tmp_path / RECORD).write_text("{not json")
    assert not check(tmp_path).ok


def test_record_owned_by_the_worker_is_rejected(tmp_path):
    write_record(tmp_path)
    status = check(tmp_path, owner_of=lambda p: WORKER)
    assert not status.ok and any("did not write it" in r for r in status.reasons)


def test_record_writable_by_the_worker_is_rejected(tmp_path):
    path = write_record(tmp_path)
    os.chmod(path, 0o666)
    status = check(tmp_path)
    assert not status.ok and any("writable" in r for r in status.reasons)


@pytest.mark.parametrize(
    "overrides,needle",
    [
        ({"ok": False}, "boundary check did not pass"),
        (
            {"live": {"ok": False, "checked_at": NOW.isoformat(), "source": "root-probe"}},
            "live probe",
        ),
        (
            {"static_checked_at": (NOW - timedelta(hours=37)).isoformat()},
            "static boundary check is older",
        ),
        (
            {
                "live": {
                    "ok": True,
                    "checked_at": (NOW - timedelta(hours=37)).isoformat(),
                    "source": "root-probe",
                }
            },
            "live probe is older",
        ),
        ({"live": {"ok": True, "checked_at": None, "source": "root-probe"}}, "live probe is older"),
        (
            {"live": {"ok": True, "checked_at": NOW.isoformat(), "source": "referee"}},
            "not from the root probe",
        ),
        ({"worker_uid": 3000}, "not the verified worker user"),
    ],
)
def test_record_content_failures(tmp_path, overrides, needle):
    write_record(tmp_path, **overrides)
    status = check(tmp_path)
    assert not status.ok and any(needle in r for r in status.reasons), status.reasons


def test_a_months_old_live_probe_never_passes(tmp_path):
    write_record(
        tmp_path,
        live={
            "ok": True,
            "checked_at": (NOW - timedelta(days=90)).isoformat(),
            "source": "root-probe",
        },
    )
    assert not check(tmp_path).ok


def test_running_as_the_referee_is_refused(tmp_path):
    write_record(tmp_path, worker_uid=REFEREE)
    status = check(tmp_path, uid=REFEREE, gids=[REFEREE])
    assert not status.ok and any("referee's user" in r for r in status.reasons)


def test_working_sudo_is_refused(tmp_path):
    write_record(tmp_path)
    status = check(tmp_path, sudo_works=lambda: True)
    assert not status.ok and any("sudo" in r for r in status.reasons)


def test_a_readable_protected_path_is_refused(tmp_path):
    write_record(tmp_path)
    status = check(tmp_path, can_read=lambda p: True)
    assert not status.ok and any("can read" in r for r in status.reasons)


async def test_meta_draft_refuses_without_isolation(tmp_path):
    from trendlab.config.schema import AppConfig
    from trendlab.engine import jobs

    config = AppConfig()
    config.improvement.control_dir = str(tmp_path)
    res = await jobs.meta_draft(None, config)
    assert res["ok"] is False and res["reason"].startswith("isolation not verified (IL-0)")
