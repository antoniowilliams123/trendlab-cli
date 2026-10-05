"""Loop detection (spec §48). Tracks repeated identical tool calls with identical results."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


@dataclass
class LoopDetector:
    threshold: int = 3
    _calls: Counter = field(default_factory=Counter)
    _results: Counter = field(default_factory=Counter)
    _failures: Counter = field(default_factory=Counter)
    strikes: int = 0

    def record(self, tool: str, fingerprint: str, output: str, ok: bool) -> str | None:
        """Return a NO_PROGRESS description when a repetition threshold is crossed."""
        call_key = _digest(tool, fingerprint)
        result_key = _digest(tool, fingerprint, output[:2000])
        self._calls[call_key] += 1
        self._results[result_key] += 1
        reason = None
        if self._results[result_key] >= self.threshold:
            reason = (
                f"the same {tool} call produced the same result {self._results[result_key]} times"
            )
        if not ok and tool in {"shell", "run_tests"}:
            fail_key = _digest(tool, output[-1500:])
            self._failures[fail_key] += 1
            if self._failures[fail_key] >= self.threshold:
                reason = (
                    reason or f"the same {tool} failure recurred {self._failures[fail_key]} times"
                )
        if reason:
            self.strikes += 1
            self._results[result_key] = 0
        return reason
