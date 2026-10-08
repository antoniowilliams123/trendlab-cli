"""Specification traceability (uplift U19): does the code do what the spec says?

``extract`` turns a spec into numbered requirements (a model call; plain "- " / "R1:" lists are
read directly). ``check`` retrieves the code and tests most related to each requirement and asks,
a few requirements per call, whether each is implemented, partly implemented or missing, and
whether a test covers it — with path:line evidence that is then verified against the files.
Saved results let the next check report *drift*: requirements that were implemented and are not
any more.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

EXTRACT = """Turn this specification into a list of testable requirements: one behaviour each,
in plain words, keeping the spec's own numbering when it has one.

Answer with ONE JSON object and nothing else:
{{"requirements": [{{"id": "R1", "text": "..."}}]}}

## Specification
{spec}
"""

CHECK = """For each requirement, decide from the code below whether the project implements it.

status: "implemented" (the behaviour is there), "partial" (some of it), "missing" (not there).
tested: true only if a test in the code below exercises it.
evidence: path:line references from the code shown (empty for missing).

Answer with ONE JSON object and nothing else:
{{"results": [{{"id": "R1", "status": "implemented" | "partial" | "missing",
  "tested": true | false, "evidence": ["path:line"], "note": "one sentence"}}]}}

## Requirements
{reqs}

## Code (most related chunks per requirement)
{code}
"""

_JSON = re.compile(r"\{.*\}", re.S)
_LIST = re.compile(r"^\s*(?:[-*]|(R\d+)[:.)]|\d+[.)])\s+(.+)$")
Call = Callable[[list[dict[str, Any]]], Awaitable[str]]


def _obj(text: str, key: str) -> list | None:
    m = _JSON.search(text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    val = obj.get(key)
    return val if isinstance(val, list) else None


def parse_requirements(text: str) -> list[dict[str, str]] | None:
    items = _obj(text, "requirements")
    if items is None:
        return None
    out = []
    for i, r in enumerate(items, 1):
        if isinstance(r, dict) and str(r.get("text") or "").strip():
            out.append({"id": str(r.get("id") or f"R{i}"), "text": str(r["text"]).strip()[:400]})
    return out or None


def listed_requirements(spec: str) -> list[dict[str, str]]:
    """A spec written as a list needs no model call."""
    out = []
    for line in spec.splitlines():
        m = _LIST.match(line)
        if m and len(m.group(2).split()) >= 4:
            text, rid = m.group(2).strip(), m.group(1)
            inner = re.match(r"(R\d+)[:.)]\s+(.+)$", text)
            if inner:
                rid, text = inner.group(1), inner.group(2)
            out.append({"id": rid or f"R{len(out) + 1}", "text": text})
    return out


async def extract(call: Call, spec: str) -> list[dict[str, str]]:
    from trendlab.providers.structured_json import ask_json

    listed = listed_requirements(spec)
    if len(listed) >= 2:
        return listed
    got, _ = await ask_json(
        call, [{"role": "user", "content": EXTRACT.format(spec=spec[:20_000])}], parse_requirements
    )
    return got or []


def parse_results(text: str) -> list[dict[str, Any]] | None:
    items = _obj(text, "results")
    if items is None:
        return None
    out = []
    for r in items:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        status = str(r.get("status") or "missing").lower()
        out.append(
            {
                "id": str(r["id"]),
                "status": status if status in {"implemented", "partial", "missing"} else "missing",
                "tested": bool(r.get("tested")),
                "evidence": [str(e) for e in r.get("evidence") or []][:6],
                "note": str(r.get("note") or "")[:300],
            }
        )
    return out


def _chunks(root: Path):
    from trendlab.context.ignore import IgnoreRules
    from trendlab.context.retrieval import CODE_EXT, chunk_file

    rules = IgnoreRules.for_project(root, respect_gitignore=True)
    out = []
    for p in rules.walk(root, max_files=1500):
        if p.suffix.lower() in CODE_EXT and p.stat().st_size < 400_000:
            out.extend(chunk_file(p.relative_to(root).as_posix(), p.read_text(errors="replace")))
    return out  # tests included: they are the evidence for "tested"


def _numbered(chunk) -> str:
    lines = chunk.text.splitlines()
    return f"# {chunk.path}\n" + "\n".join(f"{chunk.start + i}: {ln}" for i, ln in enumerate(lines))


async def check(
    call: Call, root: Path, reqs: list[dict[str, str]], *, k: int = 4, batch: int = 5
) -> dict[str, Any]:
    from trendlab.context.retrieval import search
    from trendlab.orchestration.handoff import evidence as verify_evidence
    from trendlab.providers.structured_json import ask_json

    chunks = _chunks(root)

    def is_test(path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        return path.startswith(("tests/", "test/")) or name.startswith("test_") or ".test." in name

    # implementation and tests are retrieved separately, so tests never crowd out the code
    src = [c for c in chunks if not is_test(c.path)]
    tests = [c for c in chunks if is_test(c.path)]
    results: list[dict[str, Any]] = []
    calls = 0
    for i in range(0, len(reqs), batch):
        group = reqs[i : i + batch]
        seen, code = set(), []
        for r in group:
            for ch, _s in search(src, r["text"], k=k) + search(tests, r["text"], k=2):
                key = (ch.path, ch.start)
                if key not in seen:
                    seen.add(key)
                    code.append(_numbered(ch))
        msg = CHECK.format(
            reqs="\n".join(f"{r['id']}: {r['text']}" for r in group),
            code="\n\n".join(code)[:40_000] or "(no related code found)",
        )
        got, _ = await ask_json(call, [{"role": "user", "content": msg}], parse_results)
        calls += 1
        by_id = {g["id"]: g for g in got or []}
        for r in group:
            res = by_id.get(r["id"]) or {
                "id": r["id"],
                "status": "missing",
                "tested": False,
                "evidence": [],
                "note": "no answer",
            }
            ev = verify_evidence(" ".join(res["evidence"]), root)
            results.append(
                {**r, **res, "evidence_verified": ev["verified"], "evidence_cited": ev["cited"]}
            )
    n = len(results) or 1
    return {
        "requirements": len(results),
        "implemented": sum(1 for r in results if r["status"] == "implemented"),
        "partial": sum(1 for r in results if r["status"] == "partial"),
        "missing": [r["id"] for r in results if r["status"] == "missing"],
        "coverage": round(sum(1 for r in results if r["status"] == "implemented") / n, 3),
        "tested_share": round(sum(1 for r in results if r["tested"]) / n, 3),
        "evidence_verified_share": round(
            sum(r["evidence_verified"] for r in results)
            / max(1, sum(r["evidence_cited"] for r in results)),
            3,
        ),
        "results": results,
        "calls": calls,
    }


def drift(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[dict[str, str]]:
    """Requirements whose status got worse since the last check (specification drift)."""
    if not previous:
        return []
    rank = {"implemented": 2, "partial": 1, "missing": 0}
    before = {r["text"]: r["status"] for r in previous.get("results", [])}
    out = []
    for r in current["results"]:
        old = before.get(r["text"])
        if old and rank[r["status"]] < rank[old]:
            out.append({"id": r["id"], "text": r["text"], "was": old, "now": r["status"]})
    return out


def state_path(root: Path) -> Path:
    return root / ".trendlab" / "spec_check.json"


# built-in eval: a spec for the suite's shop repository with known answers
SHOP_SPEC = """# Shop requirements
- R1: Orders of 100 or more get 5% off and orders of 500 or more get 10% off.
- R2: An 8% tax is added to order totals by default, and callers can turn tax off.
- R3: Reserving more units than are in stock raises an error and leaves the stock unchanged.
- R4: Members get an extra 15% discount on every order.
- R5: Customers can apply discount codes such as SAVE10 at checkout.
- R6: Validation rejects order lines with a non-positive quantity or a negative price.
- R7: SKUs at or below a stock threshold (default 5) are reported for restocking.
- R8: Revenue reports can be exported as CSV files.
- R9: Order summaries show every amount with two decimals.
- R10: Orders can be saved to and loaded from JSON files.
"""
SHOP_TRUTH = {
    "R1": "implemented",
    "R2": "implemented",
    "R3": "implemented",
    "R4": "missing",
    "R5": "missing",
    "R6": "implemented",
    "R7": "implemented",
    "R8": "missing",
    "R9": "implemented",
    "R10": "partial",
}
