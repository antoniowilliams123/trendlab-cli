# trendlab/engine/isolation.py
"""IL-0 worker-side guard (evals spec §30.2, D-094, D-099).

Unattended candidate work runs only when the referee holds a fresh proof of the isolation boundary
and this process sits on the worker side of it. The referee writes ``isolation.json`` into a control
directory the worker can read but not write. Its ``live`` part comes from a root-owned probe that
runs daily as the worker user. Everything here fails closed, and a proof older than 36 hours is not
accepted.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

RECORD = "isolation.json"
MAX_AGE_S = 36 * 3600  # the root probe runs daily at 00:45 ET, the static check nightly at 01:30 ET

MAX_SKEW_S = 300  # tolerated clock skew for stamps slightly in the future


@dataclass
class IsolationStatus:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def _sudo_works() -> bool:
    try:
        result = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _can_read(path: str) -> bool:
    return os.access(path, os.R_OK) or os.access(path, os.X_OK)


def _writable_by(mode: int, owner: int, group: int, uid: int, gids: Iterable[int]) -> bool:
    """Mode-bit check only: it cannot see ACLs or read-only mounts, so the root-owned control
    directory stays the real boundary."""
    if uid == 0:
        return True
    if owner == uid:
        return bool(mode & stat.S_IWUSR)
    if group in set(gids):
        return bool(mode & stat.S_IWGRP)
    return bool(mode & stat.S_IWOTH)


def _age_s(stamp: object, now: float) -> float:
    """Seconds since an aware ISO 8601 stamp; inf (stale) for anything malformed, naive or from
    the future beyond a small clock skew."""
    if not isinstance(stamp, str):
        return float("inf")
    try:
        when = datetime.fromisoformat(stamp)
        if when.tzinfo is None:
            return float("inf")
        age = now - when.timestamp()
    except (ValueError, OverflowError, OSError):
        return float("inf")
    return float("inf") if age < -MAX_SKEW_S else age


def _is_id(value: object) -> bool:
    return type(value) is int


def _read_record(path: Path, stat_of: Callable[[int | str], os.stat_result]):
    """Open without following symlinks and judge the very file we read (no TOCTOU)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = stat_of(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("not a regular file")
        with os.fdopen(fd, encoding="utf-8", closefd=False) as fh:
            record = json.loads(fh.read(1_000_000))
    finally:
        os.close(fd)
    if not isinstance(record, dict):
        raise ValueError("not an object")
    return st, record


def check_isolation(
    control_dir: str | Path,
    *,
    uid: int | None = None,
    gids: Iterable[int] | None = None,
    now: float | None = None,
    sudo_works: Callable[[], bool] = _sudo_works,
    can_read: Callable[[str], bool] = _can_read,
    stat_of: Callable[[int | str], os.stat_result] = os.stat,
) -> IsolationStatus:
    uid = os.getuid() if uid is None else uid
    gids = {*os.getgroups(), os.getgid(), os.getegid()} if gids is None else set(gids)
    now = time.time() if now is None else now
    directory = Path(control_dir).expanduser()
    if not directory.is_absolute():
        return IsolationStatus(
            False, [f"the control directory is not an absolute path: {directory}"]
        )
    path = directory / RECORD
    try:
        st, record = _read_record(path, stat_of)
        dir_st = stat_of(str(directory))
    except (OSError, ValueError, RecursionError):
        return IsolationStatus(False, [f"no readable referee isolation record at {path}"])
    live = record.get("live") if isinstance(record.get("live"), dict) else {}
    owner = st.st_uid
    referee, worker = record.get("referee_uid"), record.get("worker_uid")
    reasons: list[str] = []
    if uid == 0:
        reasons.append("this process runs as root")
    if owner == uid:
        reasons.append(
            "the isolation record is owned by this user, so the referee did not write it"
        )
    elif not (owner == 0 or (_is_id(referee) and owner == referee)):
        reasons.append("the isolation record is owned by neither the referee nor root")
    if _writable_by(st.st_mode, owner, st.st_gid, uid, gids):
        reasons.append("the isolation record is writable by this user")
    if _writable_by(dir_st.st_mode, dir_st.st_uid, dir_st.st_gid, uid, gids):
        reasons.append("the control directory is writable by this user")
    if record.get("ok") is not True:
        reasons.append("the referee's boundary check did not pass")
    if live.get("ok") is not True:
        reasons.append("the live probe as the worker user has not passed")
    if live.get("source") != "root-probe":
        reasons.append("the live result is not from the root probe")
    hours = MAX_AGE_S // 3600
    if _age_s(record.get("static_checked_at"), now) > MAX_AGE_S:
        reasons.append(f"the static boundary check is older than {hours} hours")
    if _age_s(live.get("checked_at"), now) > MAX_AGE_S:
        reasons.append(f"the live probe is older than {hours} hours")
    if not (_is_id(referee) and _is_id(worker)):
        reasons.append("the record's referee_uid or worker_uid is missing or not an integer")
    elif uid == referee:
        reasons.append("this process runs as the referee's user")
    elif uid != worker:
        reasons.append("this process is not the verified worker user")
    if sudo_works():
        reasons.append("sudo works for this user")
    protected = record.get("protected_paths")
    if not (
        isinstance(protected, list)
        and protected
        and all(isinstance(p, str) and p for p in protected)
    ):
        reasons.append("protected_paths is missing or malformed")
    else:
        for item in protected:
            if can_read(item):
                reasons.append(f"this user can read a protected referee path: {item}")
    return IsolationStatus(not reasons, reasons)
