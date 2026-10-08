"""web_fetch output → status, size, title, headings and the paragraphs that match the question."""

from __future__ import annotations

import re
from typing import Any

_HEADING = re.compile(r"^#{1,3} (?P<h>.+)$", re.M)


def parse(text: str, meta: dict[str, Any]) -> Any:
    from trendlab.tools.views import ToolOutput

    if meta.get("hint") != "http":
        return None
    title = meta.get("title") or ""
    status = meta.get("status")
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    heads = [h.group("h").strip() for h in _HEADING.finditer(text)][:12]
    url = (meta.get("url") or "")[:100]
    t1 = f"fetched: {url} — HTTP {status}, {len(text)} chars, {len(paras)} paragraphs"
    if title:
        t1 += f", title: {title[:80]}"
    tier2: list[str] = []
    if heads:
        tier2.append("headings: " + " · ".join(heads))
    question = (meta.get("question") or "").lower().split()
    scored = []
    for p in paras:
        score = sum(1 for w in question if len(w) > 3 and w in p.lower())
        scored.append((score, p))
    scored.sort(key=lambda t: -t[0])
    picked = [p for s, p in scored[:4] if s > 0] or paras[:3]
    for p in picked:
        tier2.append(p[:400])
    return ToolOutput(
        tier1=t1,
        tier2="\n\n".join(tier2) or None,
        kind="http",
        stats={"status": status, "paragraphs": len(paras)},
    )
