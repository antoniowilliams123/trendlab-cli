"""ruff / eslint / tsc / pyright / mypy / go vet output → tiers grouped by file and code."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

# file:line:col: CODE message   (ruff, mypy, pyright-ish, go vet)
_PY = re.compile(
    r"^(?P<file>[\w./\\-]+\.(?:py|go|rs)):(?P<line>\d+)(?::(?P<col>\d+))?:?\s+"
    r"(?P<code>[A-Z]{1,5}\d{2,4}|error|warning|note)?\s*:?\s*(?P<msg>.+)$",
    re.M,
)
# tsc: file.ts(12,5): error TS2322: message
_TSC = re.compile(
    r"^(?P<file>[\w./\\-]+\.tsx?)\((?P<line>\d+),(?P<col>\d+)\): error "
    r"(?P<code>TS\d+): (?P<msg>.+)$",
    re.M,
)
# eslint: "  12:5  error  message  rule-name"
_ESLINT = re.compile(
    r"^\s+(?P<line>\d+):(?P<col>\d+)\s+(?P<sev>error|warning)\s+(?P<msg>.+?)"
    r"\s{2,}(?P<code>[\w@/-]+)$",
    re.M,
)
_ESLINT_FILE = re.compile(r"^(?P<file>/?[\w./\\-]+\.(?:[jt]sx?|mjs|cjs))$", re.M)
_RUFF_FOUND = re.compile(r"^Found (?P<n>\d+) errors?", re.M)
_CLEAN = re.compile(r"^(All checks passed!|Success: no issues found|Found 0 errors|0 errors)", re.M)


def parse(text: str, meta: dict[str, Any]) -> Any:
    from trendlab.tools.views import ToolOutput

    findings: list[tuple[str, str, str, str]] = []  # file, line, code, msg
    for m in _TSC.finditer(text):
        findings.append((m.group("file"), m.group("line"), m.group("code"), m.group("msg")))
    current = None
    for line in text.splitlines():
        f = _ESLINT_FILE.match(line)
        if f:
            current = f.group("file")
            continue
        e = _ESLINT.match(line)
        if e and current:
            findings.append((current, e.group("line"), e.group("code"), e.group("msg")))
    for m in _PY.finditer(text):
        code = m.group("code") or ""
        if code in {"error", "warning", "note"} or re.match(r"[A-Z]{1,5}\d{2,4}$", code):
            findings.append((m.group("file"), m.group("line"), code, m.group("msg")))
    hinted = (
        meta.get("hint") == "lint" or bool(_RUFF_FOUND.search(text)) or bool(_CLEAN.search(text))
    )
    if not findings and not hinted:
        return None
    by_code = Counter(code for _f, _l, code, _m in findings)
    by_file = Counter(f for f, _l, _c, _m in findings)
    if not findings:
        t1 = "lint: clean"
    else:
        codes = ", ".join(f"{c} ×{n}" for c, n in by_code.most_common(6))
        t1 = f"lint: {len(findings)} findings in {len(by_file)} files — {codes}"
    if meta.get("exit_code") is not None:
        t1 += f", exit {meta['exit_code']}"
    tier2: list[str] = []
    for f, n in by_file.most_common(10):
        tier2.append(f"{f} ({n})")
        for _ff, line, code, msg in [x for x in findings if x[0] == f][:4]:
            tier2.append(f"  :{line} {code} {msg[:120]}")
    return ToolOutput(
        tier1=t1,
        tier2="\n".join(tier2) or None,
        kind="lint",
        stats={
            "findings": len(findings),
            "files": len(by_file),
            "by_code": dict(by_code.most_common(10)),
            "ok": not findings,
        },
    )
