"""pytest / unittest output → tiers."""

from __future__ import annotations

import re
from typing import Any

_SUMMARY = re.compile(
    r"=+ (?P<body>(?:\d+ (?:passed|failed|error|errors|skipped|xfailed|xpassed|warnings?"
    r"|deselected)[, ]*)+)"
    r"(?:\s*in (?P<secs>[\d.]+)s)?"
)
_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR) (?P<id>\S+)(?: - (?P<msg>.*))?$", re.M)
_SECTION = re.compile(r"^_{3,} (?P<name>.+?) _{3,}$", re.M)
_ASSERT = re.compile(r"^E\s+(?P<line>.+)$", re.M)
_LOC = re.compile(r"^(?P<file>[\w./\\-]+\.py):(?P<line>\d+): ", re.M)
_UNITTEST = re.compile(
    r"^Ran (?P<n>\d+) tests? in (?P<secs>[\d.]+)s\n+(?P<status>OK|FAILED)"
    r"(?: \((?P<detail>[^)]*)\))?",
    re.M,
)


def _counts(body: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for num, word in re.findall(
        r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|warnings?|deselected)", body
    ):
        key = {"error": "errors", "warning": "warnings"}.get(word, word)
        out[key] = out.get(key, 0) + int(num)
    return out


def parse(text: str, meta: dict[str, Any]) -> Any:
    from trendlab.tools.views import ToolOutput

    m = _SUMMARY.search(text)
    u = _UNITTEST.search(text) if m is None else None
    if m is None and u is None:
        return None
    if m is not None:
        counts = _counts(m.group("body"))
        secs = m.group("secs")
    else:
        assert u is not None
        n = int(u.group("n"))
        detail = u.group("detail") or ""
        failed = sum(int(x) for x in re.findall(r"(?:failures|errors)=(\d+)", detail))
        counts = {"passed": n - failed, "failed": failed}
        secs = u.group("secs")
    failed_n = counts.get("failed", 0) + counts.get("errors", 0)
    exit_code = meta.get("exit_code")
    summary = ", ".join(f"{v} {k}" for k, v in counts.items())
    t1 = f"tests: {summary}" + (f", {float(secs):.1f}s" if secs else "")
    if exit_code is not None:
        t1 += f", exit {exit_code}"
    failures = [
        (mm.group("id"), (mm.group("msg") or "").strip()) for mm in _FAILED_LINE.finditer(text)
    ]
    tier2_lines: list[str] = []
    if failures:
        t1 += f"\nfirst failing: {failures[0][0]}"
        for test_id, msg in failures[:12]:
            tier2_lines.append(f"FAIL {test_id}" + (f" — {msg[:160]}" if msg else ""))
        # assertion lines and locations from the long sections
        for sec in _SECTION.finditer(text):
            start = sec.end()
            nxt = _SECTION.search(text, start)
            block = text[start : nxt.start() if nxt else len(text)]
            asserts = [a.group("line").strip() for a in _ASSERT.finditer(block)][:3]
            loc = _LOC.search(block)
            if asserts or loc:
                tier2_lines.append(
                    f"  {sec.group('name').strip()[:80]}: "
                    + (f"{loc.group('file')}:{loc.group('line')} " if loc else "")
                    + " | ".join(a[:140] for a in asserts)
                )
            if len(tier2_lines) > 40:
                break
    return ToolOutput(
        tier1=t1,
        tier2="\n".join(tier2_lines) or None,
        kind="tests",
        stats={
            "counts": counts,
            "failed": failed_n,
            "secs": float(secs) if secs else None,
            "ok": failed_n == 0,
        },
    )
