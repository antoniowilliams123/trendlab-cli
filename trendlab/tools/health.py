"""Tool reliability (uplift U4): per-tool counters, a circuit breaker and postconditions.

A *tool failure* is an exception, a timeout or a skipped call — never a command that ran and
returned non-zero (that is a result the model needs to see). After ``threshold`` consecutive
failures a tool is paused for ``cooldown_s``; the model gets a readable reason and the names of
tools it can use instead. One probe call is allowed when the cooldown ends (half-open).
"""

from __future__ import annotations

import ast
import json
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RESULT_TOOLS = {"shell", "run_tests", "background"}  # non-zero exit is a result, not a failure
ALTERNATIVES = {
    "search_text": "glob + read_file",
    "web_fetch": "web_search or the project's own docs",
    "web_search": "web_fetch of a known URL",
    "run_tests": "shell with the test command",
    "glob": "list_directory",
    "list_directory": "glob",
}


@dataclass
class ToolStats:
    calls: int = 0
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    timeouts: int = 0
    total_ms: int = 0
    consecutive_failures: int = 0
    last_error: str = ""
    open_until: float = 0.0  # breaker open while now < open_until
    opened: int = 0

    @property
    def success_rate(self) -> float:
        return round(self.ok / self.calls, 3) if self.calls else 1.0

    @property
    def avg_ms(self) -> int:
        done = self.ok + self.failed
        return int(self.total_ms / done) if done else 0


@dataclass
class ToolHealth:
    threshold: int = 3
    cooldown_s: float = 60.0
    stats: dict[str, ToolStats] = field(default_factory=dict)

    def _s(self, tool: str) -> ToolStats:
        return self.stats.setdefault(tool, ToolStats())

    def blocked(self, tool: str, now: float | None = None) -> str | None:
        """A message when the breaker is open for ``tool``; None when calls may proceed."""
        s = self._s(tool)
        now = time.monotonic() if now is None else now
        if s.open_until and now < s.open_until:
            left = int(s.open_until - now) + 1
            alt = ALTERNATIVES.get(tool, "another tool")
            return (
                f"{tool} is paused for {left}s after {s.consecutive_failures} consecutive "
                f"failures (last: {s.last_error[:120]}). Use {alt} meanwhile."
            )
        if s.open_until and now >= s.open_until:
            s.open_until = 0.0  # half-open: let one probe through
        return None

    def record(
        self,
        tool: str,
        *,
        ok: bool,
        failed: bool,
        skipped: bool,
        ms: int,
        error: str = "",
        timed_out: bool = False,
        now: float | None = None,
    ) -> bool:
        """Returns True when this call opened the breaker."""
        s = self._s(tool)
        s.calls += 1
        s.total_ms += ms
        if skipped:
            s.skipped += 1
        if timed_out:
            s.timeouts += 1
        if failed or skipped:
            s.failed += 1 if failed else 0
            s.consecutive_failures += 1
            s.last_error = error
            if s.consecutive_failures >= self.threshold and not s.open_until:
                s.open_until = (time.monotonic() if now is None else now) + self.cooldown_s
                s.opened += 1
                return True
            return False
        if ok:
            s.ok += 1
        s.consecutive_failures = 0
        s.open_until = 0.0
        return False

    def summary(self) -> dict[str, dict[str, Any]]:
        return {
            t: {
                "calls": s.calls,
                "success_rate": s.success_rate,
                "failed": s.failed,
                "skipped": s.skipped,
                "timeouts": s.timeouts,
                "avg_ms": s.avg_ms,
                "breaker_opened": s.opened,
            }
            for t, s in sorted(self.stats.items())
        }

    def overall_success_rate(self) -> float:
        calls = sum(s.calls for s in self.stats.values())
        ok = sum(s.ok for s in self.stats.values())
        return round(ok / calls, 3) if calls else 1.0


def postcondition(path: Path) -> str | None:
    """Cheap structural checks after an edit: the file must still parse. Returns the problem."""
    try:
        if path.suffix == ".py":
            ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        elif path.suffix == ".json":
            json.loads(path.read_text(encoding="utf-8", errors="replace"))
        elif path.suffix == ".toml":
            tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:
        return f"{path.name}:{exc.lineno}: {exc.msg}"
    except (ValueError, tomllib.TOMLDecodeError) as exc:
        return f"{path.name}: {str(exc)[:120]}"
    except OSError:
        return None
    return None
