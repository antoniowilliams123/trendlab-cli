"""Locked runtime (evals spec §30.2, IL-0).

When ``TRENDLAB_RUNTIME_DIR`` is set (by the root-owned systemd units of the VPS worker), every
input that shapes the live agent's behavior comes from referee-owned, read-only places:
- config, skills, commands, plugins and model drivers from the runtime dir;
- project instructions, memory and project skills from ``TRENDLAB_PROJECT_CONTEXT``, the production
  checkout.
The worker's writable dev clone and home keep state only, so editing them cannot change the live
agent; such edits reach production only through the candidate, eval and promotion lifecycle.
"""

from __future__ import annotations

import os
from pathlib import Path

RUNTIME_ENV = "TRENDLAB_RUNTIME_DIR"
CONTEXT_ENV = "TRENDLAB_PROJECT_CONTEXT"


class LockedError(Exception):
    """A write to a referee-owned behavioral input was refused (IL-0)."""


def refuse_if_locked(what: str) -> None:
    if locked():
        raise LockedError(f"the runtime is locked (IL-0): {what} cannot be changed here")


def runtime_dir() -> Path | None:
    value = os.environ.get(RUNTIME_ENV)
    return Path(value).expanduser() if value else None


def locked() -> bool:
    return runtime_dir() is not None


def isolated_home() -> Path:
    """Behavior home for an isolated harness app (a benchmark fixture) that names no home of its
    own: referee-owned and normally absent, so the run sees no home skills, commands, plugins or
    drivers."""
    rt = runtime_dir()
    assert rt is not None
    return rt / "isolated-home"


def behavior_home(state_home: Path) -> Path:
    """Where global skills, commands, plugins and model drivers are read from."""
    return runtime_dir() or state_home


def context_root(project_root: Path) -> Path:
    """Where project instructions, memory, project skills, commands and drivers are read from."""
    rt = runtime_dir()
    if rt is None:
        return project_root
    ctx = os.environ.get(CONTEXT_ENV)
    return Path(ctx).expanduser() if ctx else rt / "project"
