"""Regenerate the rated vocabulary file from the rating ledger.

The ledger (docs/ratings/ledger.json) maps term number → {"rating": int | "n/a", "evidence": str}.
Original ratings come from the first rated copy; every change must name its evidence (a commit,
a measurement, or a test). Output: the rated vocabulary with "x/10 → y/10" on changed terms and
a header with before/after means.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path

RATED = Path.home() / "AI Engineer" / "AI_Engineering_Master_Vocabulary_TrendLab_Rated.md"
LEDGER = Path(__file__).resolve().parent.parent / "docs" / "ratings" / "ledger.json"
ORIGINAL = Path(__file__).resolve().parent.parent / "docs" / "ratings" / "original_ratings.json"
TAG = re.compile(r"\s*\*\*\[TrendLab CLI: [^\]]*\]\*\*\s*$|\s*\*\[TrendLab CLI: n/a[^\]]*\]\*\s*$")


def main() -> None:
    text = RATED.read_text(encoding="utf-8")
    item = re.compile(r"^(\d+)\. \*\*(.+?)\*\* — (.*)$")
    if not ORIGINAL.exists():  # snapshot the first ratings once
        orig = {}
        for line in text.splitlines():
            m = item.match(line)
            if m:
                r = re.search(r"\[TrendLab CLI: (\d+)/10", line)
                na = "[TrendLab CLI: n/a" in line
                orig[m.group(1)] = int(r.group(1)) if r else ("n/a" if na else None)
        ORIGINAL.write_text(json.dumps(orig, indent=0))
    orig = json.loads(ORIGINAL.read_text())
    ledger = json.loads(LEDGER.read_text()) if LEDGER.exists() else {}
    out, before, after = [], [], []
    body = text.split("\n\n", 1)[1] if text.startswith("> **TrendLab CLI rating copy") else text
    for line in body.splitlines():
        m = item.match(line)
        if not m:
            out.append(line)
            continue
        n = m.group(1)
        # everything from the first rating tag on is regenerated (tags + evidence)
        base = line.split(" **[TrendLab CLI:", 1)[0].split(" *[TrendLab CLI: n/a", 1)[0]
        o = orig.get(n)
        entry = ledger.get(n)
        new = entry["rating"] if entry else o
        if o is None and entry is None:
            out.append(base)
            continue
        if new == "n/a":
            out.append(base + " *[TrendLab CLI: n/a — not a harness concern by design]*")
            continue
        if isinstance(o, int):
            before.append(o)
        after.append(new)
        if entry and isinstance(o, int) and new != o:
            out.append(f"{base} **[TrendLab CLI: {o}/10 → {new}/10]** — {entry['evidence']}")
        else:
            out.append(f"{base} **[TrendLab CLI: {new}/10]**")
    below = sum(1 for x in after if x < 9)
    header = (
        "> **TrendLab CLI rating copy.** AI-related terms carry `[TrendLab CLI: x/10]`; changed terms show "
        "`old → new` and the evidence (commit, measurement or test) behind the change. "
        "Terms that are general computer science are unrated; model-training or market terms are n/a.\n"
        f"> Rated: {len(after)} · mean before {statistics.mean(before):.1f} → now {statistics.mean(after):.1f} · "
        f"at 9 or above: {len(after) - below} · below 9: {below}.\n"
    )
    RATED.write_text(header + "\n" + "\n".join(out) + "\n", encoding="utf-8")
    print(
        f"rated {len(after)} · mean {statistics.mean(before):.2f} → {statistics.mean(after):.2f} · below 9: {below}"
    )
    if "--list-below" in sys.argv:
        for line in out:
            m = item.match(line)
            r = re.search(r"(\d+)/10\]\*\*", line)
            if m and r and int(r.group(1)) < 9:
                print(f"  {m.group(1)} {m.group(2)}: {r.group(1)}")


if __name__ == "__main__":
    main()
