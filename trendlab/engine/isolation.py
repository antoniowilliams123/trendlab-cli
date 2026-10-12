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


def _owner_of(path: str) -> int:
    return os.stat(path).st_uid


def _writable_by(mode: int, owner: int, group: int, uid: int, gids: Iterable[int]) -> bool:
    if owner == uid:
        return bool(mode & stat.S_IWUSR)
    if group in set(gids):
        return bool(mode & stat.S_IWGRP)
    return bool(mode & stat.S_IWOTH)


def _age_s(stamp: object, now: float) -> float:
    try:
        return now - datetime.fromisoformat(str(stamp)).timestamp()
    except (TypeError, ValueError):
        return float("inf")


def check_isolation(
    control_dir: str | Path,
    *,
    uid: int | None = None,
    gids: Iterable[int] | None = None,
    now: float | None = None,
    sudo_works: Callable[[], bool] = _sudo_works,
    can_read: Callable[[str], bool] = _can_read,
    owner_of: Callable[[str], int] = _owner_of,
) -> IsolationStatus:
    uid = os.getuid() if uid is None else uid
    gids = list(os.getgroups()) if gids is None else list(gids)
    now = time.time() if now is None else now
    path = Path(control_dir).expanduser() / RECORD
    try:
        st = path.stat()
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        return IsolationStatus(False, [f"no readable referee isolation record at {path}"])
    live = record.get("live") if isinstance(record.get("live"), dict) else {}
    owner = owner_of(str(path))
    reasons: list[str] = []
    if owner == uid:
        reasons.append(
            "the isolation record is owned by this user, so the referee did not write it"
        )
    if _writable_by(st.st_mode, owner, st.st_gid, uid, gids):
        reasons.append("the isolation record is writable by this user")
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
    if uid == record.get("referee_uid"):
        reasons.append("this process runs as the referee's user")
    elif uid != record.get("worker_uid"):
        reasons.append("this process is not the verified worker user")
    if sudo_works():
        reasons.append("sudo works for this user")
    for protected in record.get("protected_paths") or []:
        if can_read(str(protected)):
            reasons.append(f"this user can read a protected referee path: {protected}")
    return IsolationStatus(not reasons, reasons)
