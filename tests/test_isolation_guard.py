# tests/test_isolation_guard.py
"""IL-0 worker guard (evals spec §30.2, D-094, D-099): unattended candidate work needs a fresh,
referee-written proof of the boundary."""

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from trendlab.engine.isolation import RECORD, _writable_by, check_isolation

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


def fake_stat(owner=REFEREE):
    """Real mode bits and type, with the owner swapped (the test user is not the referee)."""

    def stat_of(target):
        st = os.stat(target)
        return SimpleNamespace(st_uid=owner, st_gid=st.st_gid, st_mode=st.st_mode)

    return stat_of


def check(tmp_path, **kw):
    kw.setdefault("uid", WORKER)
    kw.setdefault("gids", [WORKER])
    kw.setdefault("now", NOW.timestamp())
    kw.setdefault("sudo_works", lambda: False)
    kw.setdefault("can_read", lambda p: False)
    kw.setdefault("stat_of", fake_stat())
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
    status = check(tmp_path, stat_of=fake_stat(WORKER))
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
        ({"referee_uid": True}, "not an integer"),
        ({"worker_uid": 2001.0}, "not an integer"),
        ({"worker_uid": None}, "not an integer"),
        ({"static_checked_at": "2099-01-01T00:00:00+00:00"}, "static boundary check is older"),
        ({"static_checked_at": "2026-10-12T09:00:00"}, "static boundary check is older"),
        ({"static_checked_at": 1760000000}, "static boundary check is older"),
        (
            {
                "live": {
                    "ok": True,
                    "checked_at": "2099-01-01T00:00:00+00:00",
                    "source": "root-probe",
                }
            },
            "live probe is older",
        ),
        (
            {"live": {"ok": True, "checked_at": "2026-10-12T11:00:00", "source": "root-probe"}},
            "live probe is older",
        ),
        ({"live": "nope"}, "live probe"),
        ({"protected_paths": []}, "protected_paths"),
        ({"protected_paths": "/srv/referee"}, "protected_paths"),
        ({"protected_paths": 5}, "protected_paths"),
        ({"protected_paths": [""]}, "protected_paths"),
        ({"protected_paths": [5]}, "protected_paths"),
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


def test_missing_protected_paths_fails_closed(tmp_path):
    path = write_record(tmp_path)
    record = json.loads(path.read_text())
    del record["protected_paths"]
    path.write_text(json.dumps(record))
    status = check(tmp_path)
    assert not status.ok and any("protected_paths" in r for r in status.reasons)


def test_small_future_skew_is_tolerated(tmp_path):
    write_record(tmp_path, static_checked_at=(NOW + timedelta(seconds=120)).isoformat())
    assert check(tmp_path).ok


def test_record_owned_by_a_third_user_is_rejected(tmp_path):
    write_record(tmp_path)
    status = check(tmp_path, stat_of=fake_stat(4242))
    assert not status.ok and any("neither the referee nor root" in r for r in status.reasons)


def test_record_owned_by_root_is_accepted(tmp_path):
    write_record(tmp_path)
    assert check(tmp_path, stat_of=fake_stat(0)).ok


def test_a_writable_control_dir_is_rejected(tmp_path):
    write_record(tmp_path)
    os.chmod(tmp_path, 0o777)
    status = check(tmp_path)
    assert not status.ok and any("control directory is writable" in r for r in status.reasons)


def test_root_is_refused(tmp_path):
    write_record(tmp_path, worker_uid=0)
    status = check(tmp_path, uid=0, gids=[0])
    assert not status.ok and any("root" in r for r in status.reasons)


def test_a_symlinked_record_fails_closed(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    write_record(real)
    (tmp_path / RECORD).symlink_to(real / RECORD)
    status = check(tmp_path)
    assert not status.ok and "no readable referee isolation record" in status.reasons[0]


def test_a_directory_record_fails_closed(tmp_path):
    (tmp_path / RECORD).mkdir()
    assert not check(tmp_path).ok


def test_deeply_nested_json_fails_closed(tmp_path):
    (tmp_path / RECORD).write_text("[" * 100000)
    assert not check(tmp_path).ok


def test_a_relative_control_dir_is_refused():
    status = check_isolation("relative/dir", uid=WORKER, gids=[WORKER])
    assert not status.ok and "absolute" in status.reasons[0]


@pytest.mark.parametrize("who", ["owner", "group", "other"])
@pytest.mark.parametrize("bit_on", [True, False])
def test_writable_by_each_class(who, bit_on):
    uid, gids = 10, [20]
    owner, group = (10, 99) if who == "owner" else (11, 20) if who == "group" else (11, 99)
    bit = {"owner": stat.S_IWUSR, "group": stat.S_IWGRP, "other": stat.S_IWOTH}[who]
    # the other two classes' bits are always set, so only the matching class decides
    others = (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) & ~bit
    mode = others | (bit if bit_on else 0)
    assert _writable_by(mode, owner, group, uid, gids) is bit_on


def test_writable_by_root_is_always_true():
    assert _writable_by(0o444, 1, 1, 0, [0])


async def test_meta_draft_default_pick_skips_evals_and_observe_items(tmp_path):
    from trendlab.config.schema import AppConfig
    from trendlab.engine import jobs
    from trendlab.engine.inbox import Inbox
    from trendlab.engine.meta import HARNESS_PROJECT

    inbox = Inbox(tmp_path / "inbox.db")
    for source in ("evals", "observe"):
        inbox.record(
            project=HARNESS_PROJECT, title=f"{source} item", signature=source, source=source
        )
    config = AppConfig()
    config.engine.harness_root = str(tmp_path)
    res = await jobs.meta_draft(inbox, config, allow_unisolated=True)
    assert res == {"ok": False, "reason": "no open harness cards"}
