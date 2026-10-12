"""IL-B module-origin pytest plugin (evals spec §30.3, D-115).

Loaded by ``test_bootstrap.py`` from the production checkout, which the candidate cannot change.
At collection finish and at session finish it checks where every loaded ``trendlab`` module came
from and writes the ones from outside the candidate's worktree to the JSON file named by
``TLE_MODULE_ORIGIN_OUT``. The runner reads that file: any foreign module fails the candidate. It
checks, it does not prove: code inside the test process could still forge the report, which is
why the referee re-runs the tests itself in IL-C.
"""

from __future__ import annotations

import json
import os
import sys

WORKTREE: str | None = None  # set by the bootstrap


def _inside(path: str, root: str) -> bool:
    real = os.path.realpath(path)
    return real == root or real.startswith(root + os.sep)


def foreign_modules(root: str) -> list[list[str]]:
    root = os.path.realpath(root)
    out = []
    for name, mod in list(sys.modules.items()):
        if name != "trendlab" and not name.startswith("trendlab."):
            continue
        where = getattr(mod, "__file__", None)
        if where:
            if not _inside(where, root):
                out.append([name, where])
            continue
        paths = list(getattr(mod, "__path__", None) or [])
        if not paths or not all(_inside(p, root) for p in paths):
            out.append([name, ";".join(paths) or "<no file>"])
    return sorted(out)


def _write(stage: str) -> None:
    target = os.environ.get("TLE_MODULE_ORIGIN_OUT")
    root = WORKTREE or os.environ.get("TLE_MODULE_ORIGIN_ROOT")
    if not target or not root:
        return  # no report: the runner treats a missing report as a failure
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[stage] = foreign_modules(root)
    tmp = f"{target}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, target)


def pytest_collection_finish(session) -> None:
    _write("collection")


def pytest_sessionfinish(session, exitstatus) -> None:
    _write("session")
