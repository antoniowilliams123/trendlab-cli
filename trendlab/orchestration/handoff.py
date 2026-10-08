"""Sub-agent handoff contract (uplift U15).

Each role must hand back fixed sections, and every ``path:line`` it cites as evidence is checked
against the repository. The parent sees the result in the report header ("evidence 4/5
verified; missing ROOT CAUSE"), so it knows how far to trust a handoff before acting on it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

REQUIRED = {
    "explorer": ("SUMMARY", "FILES", "EVIDENCE"),
    "debugger": ("ROOT CAUSE", "EVIDENCE", "PROPOSED FIX"),
    "tester": ("COMMANDS RUN", "RESULTS"),
    "reviewer": ("VERDICT", "ISSUES"),
}
_REF = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.[A-Za-z]{1,5}):(\d{1,6})\b")


def missing_sections(role: str, text: str) -> list[str]:
    up = (text or "").upper()
    return [s for s in REQUIRED.get(role, ()) if s not in up]


_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".trendlab"}


def _resolve(path: str, root: Path) -> Path:
    """The cited file; a bare name ("report.py") resolves only when exactly one project file
    has that name, so an ambiguous or wrong citation stays unverified."""
    f = (root / path).resolve()
    if f.is_file() or "/" in path:
        return f
    hits = [
        h
        for h in root.rglob(path)
        if h.is_file() and not _SKIP.intersection(h.relative_to(root).parts)
    ][:2]
    return hits[0].resolve() if len(hits) == 1 else f


def evidence(text: str, root: Path) -> dict[str, Any]:
    """Cited ``path:line`` references and how many point at a real line of a real file."""
    refs = sorted({(p, int(n)) for p, n in _REF.findall(text or "")})
    ok = []
    for path, line in refs:
        f = _resolve(path, root)
        try:
            inside = root.resolve() in f.parents or f == root.resolve()
            n_lines = (
                len(f.read_text(errors="ignore").splitlines()) if inside and f.is_file() else 0
            )
        except OSError:
            n_lines = 0
        if 1 <= line <= n_lines:
            ok.append(f"{path}:{line}")
    return {
        "cited": len(refs),
        "verified": len(ok),
        "unverified": [f"{p}:{n}" for p, n in refs if f"{p}:{n}" not in ok][:10],
    }


def check(role: str, text: str, root: Path) -> dict[str, Any]:
    ev = evidence(text, root)
    missing = missing_sections(role, text)
    return {
        "missing_sections": missing,
        "evidence_cited": ev["cited"],
        "evidence_verified": ev["verified"],
        "unverified": ev["unverified"],
        "complete": not missing and ev["verified"] == ev["cited"],
    }


def header(result: dict[str, Any]) -> str:
    parts = []
    if result["evidence_cited"]:
        parts.append(f"evidence {result['evidence_verified']}/{result['evidence_cited']} verified")
    else:
        parts.append("no path:line evidence cited")
    if result["missing_sections"]:
        parts.append("missing " + ", ".join(result["missing_sections"]))
    if result["unverified"]:
        parts.append("check " + ", ".join(result["unverified"][:3]))
    return "; ".join(parts)
