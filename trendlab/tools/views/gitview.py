"""git diff / git status / git log output → files and counts, hunk headers."""

from __future__ import annotations

import re
from typing import Any

_DIFF_FILE = re.compile(r"^diff --git a/(?P<a>\S+) b/(?P<b>\S+)$", re.M)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<len>\d+))? @@(?P<ctx>.*)$", re.M)
_STATUS = re.compile(r"^(?P<code>[ MADRCU?!]{2}) (?P<path>.+)$", re.M)
_LOG = re.compile(r"^(?P<sha>[0-9a-f]{7,40}) (?P<subject>.+)$", re.M)


def parse(text: str, meta: dict[str, Any]) -> Any:
    from trendlab.tools.views import ToolOutput

    cmd = (meta.get("command") or "").strip()
    hint = meta.get("hint")
    files = list(_DIFF_FILE.finditer(text))
    if files and (hint == "diff" or cmd.startswith("git diff") or "diff --git" in text[:200]):
        added = sum(
            1 for ln in text.splitlines() if ln.startswith("+") and not ln.startswith("+++")
        )
        removed = sum(
            1 for ln in text.splitlines() if ln.startswith("-") and not ln.startswith("---")
        )
        t1 = f"diff: {len(files)} files, +{added} -{removed}"
        tier2: list[str] = []
        for f in files[:15]:
            start = f.end()
            nxt = _DIFF_FILE.search(text, start)
            block = text[start : nxt.start() if nxt else len(text)]
            hunks = []
            for h in list(_HUNK.finditer(block))[:4]:
                ctx = h.group("ctx").strip()
                hunks.append(f"@{h.group('start')}" + (f" {ctx}" if ctx else ""))
            a = sum(
                1 for ln in block.splitlines() if ln.startswith("+") and not ln.startswith("+++")
            )
            r = sum(
                1 for ln in block.splitlines() if ln.startswith("-") and not ln.startswith("---")
            )
            tier2.append(f"{f.group('b')} +{a} -{r} " + " ".join(hunks))
        return ToolOutput(
            tier1=t1,
            tier2="\n".join(tier2),
            kind="diff",
            stats={"files": len(files), "added": added, "removed": removed},
        )
    if cmd.startswith("git status") or hint == "status":
        rows = [(m.group("code"), m.group("path")) for m in _STATUS.finditer(text)]
        if len(rows) < 20 and hint != "status":
            return None
        kinds = {}
        for code, _p in rows:
            k = {
                "M": "modified",
                "A": "added",
                "D": "deleted",
                "?": "untracked",
                "R": "renamed",
            }.get(code.strip()[:1], "other")
            kinds[k] = kinds.get(k, 0) + 1
        t1 = f"git status: {len(rows)} paths — " + ", ".join(f"{v} {k}" for k, v in kinds.items())
        tier2 = "\n".join(f"{c} {p}" for c, p in rows[:40])
        return ToolOutput(tier1=t1, tier2=tier2, kind="status", stats={"paths": len(rows), **kinds})
    if cmd.startswith("git log") or hint == "log":
        rows = list(_LOG.finditer(text))
        if len(rows) < 25 and hint != "log":
            return None
        t1 = f"git log: {len(rows)} commits"
        if rows:
            t1 += f"; newest {rows[0].group('sha')[:7]} {rows[0].group('subject')[:80]}"
        tier2 = "\n".join(f"{r.group('sha')[:7]} {r.group('subject')[:100]}" for r in rows[:30])
        return ToolOutput(tier1=t1, tier2=tier2, kind="log", stats={"commits": len(rows)})
    return None
