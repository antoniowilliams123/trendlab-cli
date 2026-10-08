"""Prompt-level commits (uplift U9): one commit per completed turn on a side branch.

Built with git plumbing and a private index file, so the user's HEAD, branch, index and working
tree are never touched: ``git add -A`` into a temporary index → ``write-tree`` → ``commit-tree``
with the previous turn (or HEAD) as parent → ``update-ref refs/heads/trendlab/turns/<session>``.
Each commit message carries the prompt, the outcome and the validation, so ``git log -p`` on the
branch is a turn-by-turn review of what the agent did.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def branch_for(session_id: str) -> str:
    return f"trendlab/turns/{session_id[:12]}"


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    out = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=60,
    )
    return out.stdout.strip()


def _maybe(root: Path, *args: str) -> str:
    try:
        return _git(root, *args)
    except subprocess.CalledProcessError:
        return ""


def is_repo(root: Path) -> bool:
    try:
        return _git(root, "rev-parse", "--is-inside-work-tree") == "true"
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def commit_turn(
    root: Path, session_id: str, *, prompt: str, result: Any, turn: int
) -> dict[str, Any] | None:
    """Commit the working tree as this turn's snapshot. None when nothing changed since the
    previous turn (or this is not a git repository)."""
    if root.resolve() == Path.home().resolve() or not is_repo(root):
        return None  # never snapshot a home directory
    branch = branch_for(session_id)
    ref = f"refs/heads/{branch}"
    parent = _maybe(root, "rev-parse", "--verify", "-q", ref) or _maybe(
        root, "rev-parse", "--verify", "-q", "HEAD"
    )
    with tempfile.TemporaryDirectory(prefix="trendlab-turn-") as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if parent:
            _git(root, "read-tree", parent, env=env)
        _git(root, "add", "-A", env=env)  # respects .gitignore
        tree = _git(root, "write-tree", env=env)
    if parent and _git(root, "rev-parse", f"{parent}^{{tree}}") == tree:
        return None
    first = " ".join((prompt or "").split())[:68] or "(no prompt)"
    body = [
        f"turn {turn}: {first}",
        "",
        f"status: {getattr(result, 'status', '?')}",
        f"files: {', '.join(getattr(result, 'changed_files', []) or []) or '(none)'}",
    ]
    runs = getattr(result, "validation_runs", None) or []
    if runs:
        last = runs[-1]
        body.append(f"validation: {last.get('command', '')} -> exit {last.get('exit_code')}")
    if getattr(result, "stop_reason", None):
        body.append(f"stopped: {result.stop_reason}")
    body += ["", "prompt:", (prompt or "")[:4000]]
    args = ["commit-tree", tree, "-m", "\n".join(body)]
    if parent:
        args[2:2] = ["-p", parent]
    sha = _git(root, *args)
    _git(root, "update-ref", ref, sha)
    return {"branch": branch, "sha": sha, "turn": turn}


def list_turns(root: Path, session_id: str, *, patch: bool = False) -> str:
    """``git log`` of the session's turn branch since it left HEAD (newest first)."""
    branch = branch_for(session_id)
    fmt = ["log", "--format=%h %ad %s", "--date=format:%H:%M", "-p" if patch else "--stat"]
    return _maybe(root, *fmt, branch, "--not", "HEAD")
