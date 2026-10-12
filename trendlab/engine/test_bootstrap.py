"""IL-B test bootstrap (evals spec §30.3, D-115): run a candidate's tests against its own code.

Run from the production checkout with the production venv, inside the sandbox:

    <prod>/.venv/bin/python <prod>/trendlab/engine/test_bootstrap.py <worktree> <pytest args>

pytest is imported first, from the production venv, before the worktree is on ``sys.path``. Then
editable-install finders are removed so ``trendlab`` cannot resolve to an installed checkout, the
worktree goes first on ``sys.path``, and the module-origin plugin is loaded from this production
checkout by an explicit file spec.
"""

import importlib.util
import os
import sys

import pytest


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: test_bootstrap.py <worktree> [pytest args...]", file=sys.stderr)
        return 2
    here = os.path.dirname(os.path.abspath(__file__))
    if sys.path and os.path.abspath(sys.path[0] or ".") == here:
        del sys.path[0]  # the script's own folder
    sys.meta_path[:] = [
        f for f in sys.meta_path if "Editable" not in getattr(f, "__name__", type(f).__name__)
    ]
    sys.path_hooks[:] = [
        h
        for h in sys.path_hooks
        if "editable" not in getattr(h, "__name__", type(h).__name__).lower()
    ]
    sys.path_importer_cache.clear()
    worktree = os.path.realpath(argv[0])
    sys.path.insert(0, worktree)
    spec = importlib.util.spec_from_file_location(
        "_tle_module_origin", os.path.join(here, "module_origin.py")
    )
    if spec is None or spec.loader is None:
        print("module-origin plugin not found", file=sys.stderr)
        return 2
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    plugin.WORKTREE = worktree
    return int(pytest.main(argv[1:], plugins=[plugin]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
