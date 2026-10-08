"""Directory listings (ls -l, ls, list_directory) → counts by extension, newest, largest."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

_LS_L = re.compile(
    r"^(?P<mode>[-dl][rwxst-]{9})\s+\d+\s+\S+\s+\S+\s+(?P<size>\d+)\s+(?P<date>\w{3}\s+\d{1,2}\s+[\d:]{4,5})\s+(?P<name>.+)$",
    re.M,
)


def parse(text: str, meta: dict[str, Any]) -> Any:
    from trendlab.tools.views import ToolOutput

    if meta.get("hint") != "listing":
        head = (
            (meta.get("command") or "").strip().split(" ", 1)[0].rsplit("/", 1)[-1]
            if meta.get("command")
            else ""
        )
        if head not in {"ls", "dir", "find", "tree"}:
            return None
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip() and not ln.startswith("total ")]
    if len(lines) < 25:
        return None  # small listings are fine verbatim
    long_rows = [m for m in (_LS_L.match(ln) for ln in lines) if m]
    if long_rows:
        names = [m.group("name") for m in long_rows]
        dirs = sum(1 for m in long_rows if m.group("mode").startswith("d"))
        sizes = sorted(((int(m.group("size")), m.group("name")) for m in long_rows), reverse=True)
        newest = [f"{m.group('date')} {m.group('name')}" for m in long_rows[-8:]]
        largest = [f"{s / 1_048_576:.1f} MB {n}" for s, n in sizes[:5]]
    else:
        names = lines
        dirs = sum(1 for n in names if n.endswith("/"))
        newest, largest = [], []
    exts = Counter(
        (
            n.rsplit(".", 1)[-1].lower()
            if "." in n.rsplit("/", 1)[-1] and not n.endswith("/")
            else "(none)"
        )
        for n in names
    )
    t1 = f"listing: {len(names)} entries ({dirs} dirs) — " + ", ".join(
        f"{e} ×{c}" for e, c in exts.most_common(6)
    )
    tier2: list[str] = []
    if newest:
        tier2.append("last rows (as listed): " + " | ".join(newest[-5:]))
    if largest:
        tier2.append("largest: " + " | ".join(largest))
    tier2.append("first entries: " + ", ".join(names[:15]) + (" …" if len(names) > 15 else ""))
    return ToolOutput(
        tier1=t1,
        tier2="\n".join(tier2),
        kind="listing",
        stats={"entries": len(names), "dirs": dirs, "by_ext": dict(exts.most_common(10))},
    )
