"""One live run per project checkout (uplift U26: concurrency control).

Two TrendLab sessions editing the same working tree at once corrupt each other's view of the
files. A run takes ``.trendlab/run.lock`` (pid, session, start); a second run in the same tree
is refused with who holds it and how to work in parallel safely (``--worktree``). A lock left
by a dead process is taken over.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


class RunLocked(Exception):
    def __init__(self, info: dict) -> None:
        super().__init__(
            f"another TrendLab run is working in this project (pid {info.get('pid')}, session "
            f"{info.get('session')}, since {info.get('started')}); wait for it, or use "
            "--worktree NAME to work in parallel in an isolated copy"
        )
        self.info = info


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lock_path(root: Path) -> Path:
    return root / ".trendlab" / "run.lock"


def acquire(root: Path, session_id: str) -> Path:
    path = lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        info = json.loads(path.read_text())
        pid = int(info.get("pid", 0))
        if pid and pid != os.getpid() and _alive(pid):
            raise RunLocked(info)
    except (OSError, ValueError):
        pass  # no lock, or an unreadable one
    path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "session": session_id,
                "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        )
    )
    return path


def release(root: Path) -> None:
    path = lock_path(root)
    try:
        info = json.loads(path.read_text())
        if int(info.get("pid", 0)) == os.getpid():
            path.unlink()
    except (OSError, ValueError):
        pass
