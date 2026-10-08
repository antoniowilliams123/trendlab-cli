"""Custom status line: a user command prints one line that TrendLab shows under its status bar.

Configured only in the user's own config (``~/.trendlab/config.toml``)::

    [ui]
    statusline = "~/.trendlab/statusline.sh"
    statusline_interval_s = 2

The command gets the session as JSON on stdin (model, cost, context, git branch, state…) and
its first line of output is shown, ANSI colours kept. A project's config cannot set it: opening
a repository must never run a command that repository chose. The command runs with a short
timeout, at most once per interval, off the UI thread; a failure shows a one-line hint.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from trendlab.config.loader import _read_toml, global_config_path

SAMPLE_SCRIPT = r"""#!/bin/sh
# TrendLab status line: print ONE line. The session arrives as JSON on stdin.
# Fields: model, git_branch, cost_usd, context_pct, state, mode, pending_approvals, project...
python3 -c '
import json, os, sys
s = json.load(sys.stdin)
parts = [os.path.basename(s["project"])]
if s["git_branch"]:
    parts.append("\033[35m" + s["git_branch"] + "\033[0m")
parts.append(s["model"].split(":")[-1])
parts.append("ctx %d%%" % s["context_pct"])
parts.append("$%.3f" % s["cost_usd"])
if s["pending_approvals"]:
    parts.append("\033[33m%d waiting\033[0m" % s["pending_approvals"])
print("  ·  ".join(parts))
'
"""
TIMEOUT_S = 2.0
MAX_CHARS = 400


def configured_command() -> tuple[str, float]:
    """(command, interval) from the user's global config; ('', 0) when unset."""
    ui = _read_toml(global_config_path()).get("ui") or {}
    cmd = str(ui.get("statusline") or "").strip()
    try:
        interval = max(0.5, float(ui.get("statusline_interval_s") or 2.0))
    except (TypeError, ValueError):
        interval = 2.0
    return cmd, interval


def _git_branch(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def session_info(tl: Any, *, running_s: float | None = None) -> dict[str, Any]:
    """What the status-line command receives on stdin."""
    ctx = tl.context.status() if getattr(tl, "context", None) else {}
    window = ctx.get("context_window") or 0
    used = ctx.get("estimated_tokens") or 0
    engine = getattr(tl, "engine", None)
    return {
        "session_id": getattr(tl, "session_id", ""),
        "model": getattr(tl, "model_ref", ""),
        "project": str(getattr(tl, "project_root", "")),
        "cwd": str(getattr(tl, "project_root", "")),
        "git_branch": _git_branch(Path(getattr(tl, "project_root", "."))),
        "state": tl.agent.state.state.value if getattr(tl, "agent", None) else "idle",
        "running_s": round(running_s, 1) if running_s is not None else None,
        "cost_usd": round(tl.costs.total_usd, 4) if getattr(tl, "costs", None) else 0.0,
        "context_tokens": used,
        "context_window": window,
        "context_pct": int(100 * used / window) if window else 0,
        "mode": "unsafe"
        if engine is not None and engine.unsafe
        else getattr(getattr(engine, "mode", None), "value", ""),
        "pending_approvals": len(tl.approvals.pending()) if getattr(tl, "approvals", None) else 0,
    }


async def render(command: str, info: dict[str, Any], cwd: Path) -> str:
    """Run the command with ``info`` on stdin; its first output line, or a short hint."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            os.path.expanduser(command),
            cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(
            proc.communicate(json.dumps(info).encode()), timeout=TIMEOUT_S
        )
    except TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        return f"statusline: timed out after {TIMEOUT_S:.0f}s"
    except OSError as exc:
        return f"statusline: {exc}"
    if proc.returncode != 0:
        first = (err.decode("utf-8", "replace").strip().splitlines() or ["failed"])[0]
        return f"statusline: exit {proc.returncode}: {first[:120]}"
    lines = out.decode("utf-8", "replace").splitlines()
    return lines[0][:MAX_CHARS] if lines else ""


class StatusLine:
    """Throttled, cached runner for one UI."""

    def __init__(self, command: str = "", interval: float = 2.0) -> None:
        if not command:
            command, interval = configured_command()
        self.command, self.interval = command, interval
        self.text = ""
        self._last = 0.0
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.command)

    def due(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        busy = self._task is not None and not self._task.done()
        return self.enabled and not busy and now - self._last >= self.interval

    async def refresh(self, tl: Any, *, running_s: float | None = None) -> str:
        self._last = time.monotonic()
        self.text = await render(
            self.command, session_info(tl, running_s=running_s), tl.project_root
        )
        return self.text

    def schedule(self, tl: Any, on_done, *, running_s: float | None = None) -> None:
        """Refresh in the background when due; ``on_done(text)`` runs on the loop."""
        if not self.due():
            return

        async def go():
            on_done(await self.refresh(tl, running_s=running_s))

        self._task = asyncio.get_running_loop().create_task(go())
