"""jest / vitest / mocha output → tiers."""

from __future__ import annotations

import re
from typing import Any

_JEST_TESTS = re.compile(r"^Tests:\s+(?P<body>.+)$", re.M)
_JEST_SUITES = re.compile(r"^Test Suites:\s+(?P<body>.+)$", re.M)
_JEST_TIME = re.compile(r"^Time:\s+(?P<secs>[\d.]+)\s*s", re.M)
_JEST_FAIL = re.compile(r"^\s*[●✕×] (?P<id>.+?)(?: › (?P<name>.+))?$", re.M)
_VITEST = re.compile(r"^\s*Tests\s+(?P<body>.+?)\s*$", re.M)
_MOCHA = re.compile(
    r"^\s*(?P<passing>\d+) passing(?: \([^)]*\))?(?:\n\s*(?P<pending>\d+) pending)?"
    r"(?:\n\s*(?P<failing>\d+) failing)?",
    re.M,
)
_EXPECT = re.compile(r"^\s*(Expected|Received|AssertionError|Error:|expect\()(?P<rest>.*)$", re.M)


def _counts(body: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for num, word in re.findall(
        r"(\d+) (passed|failed|skipped|todo|total|pending|passing|failing)", body
    ):
        key = {"passing": "passed", "failing": "failed"}.get(word, word)
        out[key] = int(num)
    return out


def parse(text: str, meta: dict[str, Any]) -> Any:
    from trendlab.tools.views import ToolOutput

    counts: dict[str, int] | None = None
    secs = None
    m = _JEST_TESTS.search(text)
    if m:
        counts = _counts(m.group("body"))
        t = _JEST_TIME.search(text)
        secs = float(t.group("secs")) if t else None
    else:
        v = _VITEST.search(text)
        if v and re.search(r"\d+ (passed|failed)", v.group("body")):
            counts = _counts(v.group("body"))
        else:
            mo = _MOCHA.search(text)
            if mo:
                counts = {
                    "passed": int(mo.group("passing")),
                    "failed": int(mo.group("failing") or 0),
                }
    if counts is None:
        return None
    failed_n = counts.get("failed", 0)
    t1 = "tests: " + ", ".join(f"{v} {k}" for k, v in counts.items() if k != "total")
    if secs:
        t1 += f", {secs:.1f}s"
    if meta.get("exit_code") is not None:
        t1 += f", exit {meta['exit_code']}"
    tier2: list[str] = []
    fails = [
        (f.group("id").strip(), (f.group("name") or "").strip()) for f in _JEST_FAIL.finditer(text)
    ]
    fails = [f for f in fails if f[0] and not f[0].startswith("Test")][:12]
    if fails:
        t1 += f"\nfirst failing: {fails[0][0]}" + (f" › {fails[0][1]}" if fails[0][1] else "")
        for fid, name in fails:
            tier2.append(f"FAIL {fid}" + (f" › {name}" if name else ""))
    for e in list(_EXPECT.finditer(text))[:8]:
        tier2.append("  " + e.group(0).strip()[:160])
    return ToolOutput(
        tier1=t1,
        tier2="\n".join(tier2) or None,
        kind="tests",
        stats={"counts": counts, "failed": failed_n, "secs": secs, "ok": failed_n == 0},
    )
