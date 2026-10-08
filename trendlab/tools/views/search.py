"""search_text / ripgrep / grep -n output → matches by file, top hits with one line of context."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

_HIT = re.compile(r"^(?P<file>[^:\n]+?):(?P<line>\d+)[:-](?P<text>.*)$", re.M)


def parse(text: str, meta: dict[str, Any]) -> Any:
    from trendlab.tools.views import ToolOutput

    hint = meta.get("hint") == "search"
    head = ""
    if meta.get("command"):
        head = meta["command"].strip().split(" ", 1)[0].rsplit("/", 1)[-1]
    if not hint and head not in {"rg", "grep", "ag", "ack"}:
        return None
    hits = list(_HIT.finditer(text))
    if len(hits) < 20 and not hint:
        return None
    if not hits:
        return ToolOutput(
            tier1="search: 0 matches", kind="search", stats={"matches": 0, "files": 0}
        )
    by_file = Counter(h.group("file") for h in hits)
    t1 = f"search: {len(hits)} matches in {len(by_file)} files — " + ", ".join(
        f"{f} ({n})" for f, n in by_file.most_common(5)
    )
    tier2 = [
        f"{h.group('file')}:{h.group('line')} {h.group('text').strip()[:120]}" for h in hits[:40]
    ]
    if len(hits) > 40:
        tier2.append(f"… {len(hits) - 40} more (inspect_output with a query)")
    return ToolOutput(
        tier1=t1,
        tier2="\n".join(tier2),
        kind="search",
        stats={
            "matches": len(hits),
            "files": len(by_file),
            "by_file": dict(by_file.most_common(10)),
        },
    )
