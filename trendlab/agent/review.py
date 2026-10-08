"""Code review of a branch, a range or a pull request (uplift U13).

Two kinds of pass:

* deterministic (free): secrets in added lines, new dependencies, leftover debug output,
  source changes with no test change;
* model lenses: one focused question per dimension — correctness, edge cases, structure
  (architecture and design), performance, tests, security. Focused prompts find more with a
  cheap model than one "review everything" prompt — that was the hypothesis; measured, one
  ``quick`` call over all lenses caught as much with far less noise, so it is the default and
  ``deep`` (one call per lens) is opt-in. A large diff is reviewed in file chunks.

Findings get stable ids and live in a per-project ledger (``.trendlab/reviews.json``). A
re-check asks, for each open finding, whether the current diff resolves it — that is finding
closure; a review is closed when no high or medium finding is open and validation passes.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

LENSES: dict[str, str] = {
    "correctness": "logic errors: wrong conditions, off-by-one, wrong variable, broken contracts "
    "between caller and callee, behaviour that does not match the stated intent",
    "edge_cases": "inputs the code mishandles: empty, None, zero, negative, very large, "
    "duplicates, unicode, boundaries, concurrency, error paths",
    "structure": "architecture and design: wrong layer or module, duplicated logic, needless "
    "abstraction, coupling, naming that misleads, a change that fights the existing design",
    "performance": "needless work: quadratic loops, repeated I/O or queries, unbounded memory, "
    "blocking calls in async code",
    "tests": "missing or weak tests: changed behaviour without a test, tests that would pass "
    "even if the change were reverted, assertions that check nothing",
    "security": "injection, secrets, unsafe deserialisation, path traversal, missing auth "
    "checks, data exposure",
}
CHUNK_CHARS = 24_000

PROMPT = """You review a code change. Report only real problems you can point to in the diff,
for this lens only: {lens} — {desc}.

Do not report style, formatting or naming preferences unless they cause a bug. Do not invent
problems: an empty findings list is the right answer for a good change.

Answer with ONE JSON object and nothing else:
{{"findings": [{{"file": "path", "line": 0, "issue": "what is wrong and why",
  "severity": "high" | "med" | "low", "lens": "{lens_key}"}}]}}

severity: high = wrong behaviour or a security hole; med = likely bug or real design cost;
low = minor.

## What the change is for
{intent}

## Diff
{diff}
"""

QUICK = """You review a code change across these lenses:
{lenses}

Report only real problems you can point to in the diff. No style or naming preferences unless
they cause a bug. An empty list is the right answer for a good change.

Answer with ONE JSON object and nothing else:
{{"findings": [{{"file": "path", "line": 0, "issue": "what is wrong and why",
  "severity": "high" | "med" | "low", "lens": "one of: {lens_keys}"}}]}}

## What the change is for
{intent}

## Diff
{diff}
"""

RECHECK = """Earlier review findings are listed below with ids. Look at the CURRENT diff and
say, for each id, whether the problem is now resolved.

Answer with ONE JSON object and nothing else:
{{"resolved": ["id", ...], "still_open": ["id", ...]}}

## Findings
{findings}

## Current diff
{diff}
"""

CONFIRM = """Below are findings another reviewer reported on a code change, with ids. For each,
decide whether it is a REAL defect introduced or left by this diff (wrong behaviour, security
hole, missing handling the code needs) — not a style preference, not a hypothetical about code
outside the diff, not something the diff already handles.

Answer with ONE JSON object and nothing else:
{{"keep": ["id", ...], "drop": ["id", ...]}}

## What the change is for
{intent}

## Findings
{findings}

## Diff
{diff}
"""

_JSON = re.compile(r"\{.*\}", re.S)
_DEBUG = re.compile(
    r"^\+(?!\+\+).*\b(print\(|console\.log\(|debugger;|breakpoint\(\)|pdb\.set_trace)"
)
_DEP_FILES = ("pyproject.toml", "requirements", "package.json", "go.mod", "Cargo.toml", "Gemfile")


def finding_id(f: dict[str, Any]) -> str:
    key = f"{f.get('lens')}|{f.get('file')}|{str(f.get('issue', ''))[:80].lower()}"
    return hashlib.sha1(key.encode()).hexdigest()[:8]


def parse_findings(text: str, lens: str | None = None) -> list[dict[str, Any]] | None:
    m = _JSON.search(text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(obj.get("findings"), list):
        return None
    out = []
    for f in obj["findings"]:
        if not isinstance(f, dict) or not str(f.get("issue") or "").strip():
            continue
        sev = str(f.get("severity") or "med").lower()
        line = f.get("line")
        path = str(f.get("file") or "")
        if path.startswith(("a/", "b/")):
            path = path[2:]
        item = {
            "lens": lens or str(f.get("lens") or "correctness"),
            "file": path,
            "line": int(line) if isinstance(line, int | float) else 0,
            "issue": str(f["issue"]).strip()[:500],
            "severity": sev if sev in {"high", "med", "low"} else "med",
        }
        item["id"] = finding_id(item)
        out.append(item)
    return out


def files_in(diff: str) -> list[str]:
    return sorted(set(re.findall(r"^\+\+\+ b/(.+)$", diff, re.M)))


def split_diff(diff: str) -> list[str]:
    """Per-file sections, packed into chunks under CHUNK_CHARS (large-diff review)."""
    parts = re.split(r"(?=^diff --git )", diff, flags=re.M)
    parts = [p for p in parts if p.strip()]
    if len(parts) <= 1:
        parts = re.split(r"(?=^--- (?:a/|/dev/null))", diff, flags=re.M)
        parts = [p for p in parts if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in parts:
        if cur and len(cur) + len(p) > CHUNK_CHARS:
            chunks.append(cur)
            cur = ""
        cur += p if len(p) <= CHUNK_CHARS else p[:CHUNK_CHARS] + "\n… [file diff truncated]\n"
    if cur:
        chunks.append(cur)
    return chunks or [diff]


def static_findings(diff: str) -> list[dict[str, Any]]:
    """Deterministic checks on added lines (no model call)."""
    from trendlab.security.scan import find_secrets

    out: list[dict[str, Any]] = []
    current = ""
    added_src = added_tests = False
    for ln in diff.splitlines():
        if ln.startswith("+++ b/"):
            current = ln[6:]
            continue
        if not ln.startswith("+") or ln.startswith("+++"):
            continue
        is_test = "test" in current.rsplit("/", 1)[-1] or current.startswith("tests/")
        added_tests |= is_test
        added_src |= not is_test and current.endswith((".py", ".ts", ".js", ".go", ".rs"))
        if _DEBUG.match(ln) and not is_test:
            out.append(
                _static(
                    "structure", current, "leftover debug output: " + ln[1:].strip()[:80], "low"
                )
            )
        if current.endswith(_DEP_FILES) or any(d in current for d in _DEP_FILES):
            out.append(
                _static(
                    "structure",
                    current,
                    "dependency manifest changed: confirm the new dependency is needed",
                    "low",
                )
            )
        hits = find_secrets(ln[1:])
        if hits:
            out.append(
                _static(
                    "security", current, "secret-like value added: " + "; ".join(hits[:2]), "high"
                )
            )
    if added_src and not added_tests:
        out.append(_static("tests", "", "source changed but no test was added or updated", "med"))
    seen, uniq = set(), []
    for f in out:
        if f["id"] not in seen:
            seen.add(f["id"])
            uniq.append(f)
    return uniq


def _static(lens: str, file: str, issue: str, severity: str) -> dict[str, Any]:
    f = {
        "lens": lens,
        "file": file,
        "line": 0,
        "issue": issue,
        "severity": severity,
        "static": True,
    }
    f["id"] = finding_id(f)
    return f


Call = Callable[[list[dict[str, Any]]], Awaitable[str]]


async def review_diff(
    call: Call,
    diff: str,
    *,
    intent: str = "",
    mode: str = "auto",
    lenses: list[str] | None = None,
    confirm_findings: bool = True,
) -> dict[str, Any]:
    """Run the static pass and the model lenses; returns {findings, calls, mode, chunks}."""
    import asyncio

    from trendlab.providers.structured_json import ask_json

    lenses = lenses or list(LENSES)
    lines = sum(1 for ln in diff.splitlines() if ln[:1] in "+-" and ln[:3] not in ("+++", "---"))
    chunks = split_diff(diff)
    if mode == "auto":
        # Measured on 32 seeded defects (Flash): deep and quick both caught 32/32, but deep
        # raised 1.16 findings per correct fix vs 0.47 for quick, at 6x the calls; quick plus
        # the confirmation pass gave 0.16. So auto is quick; deep stays available on request.
        mode = "quick"
    intent = intent.strip()[:3000] or "(not stated; judge the change on its own terms)"
    jobs = []
    for chunk in chunks:
        if mode == "deep":
            for key in lenses:
                msg = PROMPT.format(
                    lens=key.replace("_", " "),
                    desc=LENSES[key],
                    lens_key=key,
                    intent=intent,
                    diff=chunk,
                )
                jobs.append((key, [{"role": "user", "content": msg}]))
        else:
            msg = QUICK.format(
                lenses="\n".join(f"- {k}: {LENSES[k]}" for k in lenses),
                lens_keys=", ".join(lenses),
                intent=intent,
                diff=chunk,
            )
            jobs.append((None, [{"role": "user", "content": msg}]))

    async def one(lens, messages):
        found, _ = await ask_json(call, messages, lambda t, _l=lens: parse_findings(t, _l))
        return found or []

    results = await asyncio.gather(*(one(lens, m) for lens, m in jobs))
    findings = static_findings(diff)
    seen = {f["id"] for f in findings}
    for group in results:
        for f in group:
            if f["id"] not in seen:
                seen.add(f["id"])
                findings.append(f)
    calls = len(jobs)
    if confirm_findings:
        findings, extra = await confirm(call, findings, diff, intent)
        calls += extra
        findings = [f for f in findings if not f.get("dropped")] + [
            f for f in findings if f.get("dropped")
        ]
    order = {"high": 0, "med": 1, "low": 2}
    findings.sort(
        key=lambda f: (bool(f.get("dropped")), order[f["severity"]], f["lens"], f["file"])
    )
    return {
        "findings": findings,
        "calls": calls,
        "mode": mode,
        "chunks": len(chunks),
        "lines": lines,
    }


def parse_confirm(text: str) -> dict[str, list[str]] | None:
    m = _JSON.search(text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(obj.get("keep"), list):
        return None
    return {"keep": [str(x) for x in obj["keep"]], "drop": [str(x) for x in obj.get("drop") or []]}


async def confirm(call: Call, findings: list[dict[str, Any]], diff: str, intent: str):
    """Second opinion on medium/high model findings (precision pass). Dropped findings are
    kept in the result with ``dropped=True`` so the decision stays auditable."""
    from trendlab.providers.structured_json import ask_json

    targets = [f for f in findings if not f.get("static") and f["severity"] in {"high", "med"}]
    if not targets:
        return findings, 0
    listing = "\n".join(
        f"- {f['id']} [{f['severity']}/{f['lens']}] {f['file']}:{f.get('line', 0)}: {f['issue']}"
        for f in targets
    )
    msg = CONFIRM.format(intent=intent, findings=listing, diff=diff[: CHUNK_CHARS * 2])
    got, _ = await ask_json(call, [{"role": "user", "content": msg}], parse_confirm)
    if not got:
        return findings, 1
    drop = set(got["drop"]) - set(got["keep"])
    for f in findings:
        if f["id"] in drop:
            f["dropped"] = True
    return findings, 1


def parse_recheck(text: str) -> dict[str, list[str]] | None:
    m = _JSON.search(text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(obj.get("resolved"), list):
        return None
    return {
        "resolved": [str(x) for x in obj["resolved"]],
        "still_open": [str(x) for x in obj.get("still_open") or []],
    }


async def recheck(call: Call, findings: list[dict[str, Any]], diff: str) -> dict[str, list[str]]:
    from trendlab.providers.structured_json import ask_json

    open_ = [
        f
        for f in findings
        if f.get("status", "open") == "open" and not f.get("static") and not f.get("dropped")
    ]
    if not open_:
        return {"resolved": [], "still_open": []}
    listing = "\n".join(f"- {f['id']} [{f['severity']}] {f['file']}: {f['issue']}" for f in open_)
    msg = RECHECK.format(findings=listing, diff=diff[: CHUNK_CHARS * 2])
    got, _ = await ask_json(call, [{"role": "user", "content": msg}], parse_recheck)
    return got or {"resolved": [], "still_open": [f["id"] for f in open_]}


# -- ledger ---------------------------------------------------------------------------------


def ledger_path(root: Path) -> Path:
    return root / ".trendlab" / "reviews.json"


def load_ledger(root: Path) -> dict[str, Any]:
    p = ledger_path(root)
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {"reviews": []}


def save_review(root: Path, review: dict[str, Any]) -> dict[str, Any]:
    led = load_ledger(root)
    review.setdefault("id", hashlib.sha1(f"{time.time()}".encode()).hexdigest()[:8])
    review.setdefault("created", time.strftime("%Y-%m-%dT%H:%M:%S"))
    for f in review.get("findings", []):
        f.setdefault("status", "dropped" if f.get("dropped") else "open")
    led["reviews"] = [r for r in led["reviews"] if r["id"] != review["id"]] + [review]
    p = ledger_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(led, indent=1))
    return review


def apply_recheck(review: dict[str, Any], outcome: dict[str, list[str]], static_now: list[str]):
    """Close what the model says is resolved; a static finding closes when the static pass no
    longer reports it."""
    resolved = set(outcome.get("resolved") or [])
    for f in review.get("findings", []):
        if f.get("status", "open") != "open":
            continue
        if f.get("static"):
            if f["id"] not in static_now:
                f["status"] = "fixed"
        elif f["id"] in resolved:
            f["status"] = "fixed"
    return review


def is_closed(review: dict[str, Any], validation_ok: bool | None) -> bool:
    """Verification closure: no open high/med finding, and validation passed when it ran."""
    blocking = [
        f
        for f in review.get("findings", [])
        if f.get("status", "open") == "open"
        and not f.get("dropped")
        and f["severity"] in {"high", "med"}
    ]
    return not blocking and validation_ok is not False


def fix_prompt(review: dict[str, Any]) -> str:
    items = [
        f
        for f in review.get("findings", [])
        if f.get("status", "open") == "open" and not f.get("dropped")
    ]
    lines = [
        "A code review of the current changes found these problems. Fix each real one, add "
        "or update tests where needed, run the validation, and say which findings you did "
        "not change and why:"
    ]
    for f in items[:20]:
        where = f["file"] + (f":{f['line']}" if f.get("line") else "")
        lines.append(f"- [{f['severity']}/{f['lens']}] {where}: {f['issue']}")
    return "\n".join(lines)
