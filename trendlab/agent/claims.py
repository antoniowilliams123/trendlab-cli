"""Unsupported claims (uplift: hallucination rate).

The final answer of a run is checked against the evidence the harness collected. A claim the
evidence contradicts — "tests pass" when the last validation failed or none ran, "fixed in
x.py" when x.py was never changed, "I added a test" when no test file changed — is an
unsupported claim. The rate of those is the harness's hallucination rate for coding work.
"""

from __future__ import annotations

import re
from typing import Any

_TESTS_PASS = re.compile(
    r"\b(all\s+)?(the\s+)?(tests?|test suite|suite|checks?)\s+(now\s+)?"
    r"(pass(es|ed|ing)?|are\s+(green|passing)|succeed(s|ed)?)\b"
    r"|\b(\d+)\s+passed\b|\bgreen\b(?=.*\btests?\b)",
    re.I,
)
_ADDED_TEST = re.compile(
    r"\b(added|wrote|created|new)\b[^.\n]{0,40}\b(regression\s+)?tests?\b", re.I
)
_FILE = re.compile(r"`?([\w./-]+\.(?:py|js|ts|tsx|mjs|go|rs|java|rb|toml|json|md))`?")
_CHANGED_VERB = re.compile(r"\b(changed|modified|edited|updated|fixed|patched)\b", re.I)


def unsupported_claims(text: str, evidence: dict[str, Any]) -> list[str]:
    """``evidence``: changed_files, validation_runs (dicts with ok), test_files_changed."""
    claims: list[str] = []
    body = text or ""
    runs = evidence.get("validation_runs") or []
    changed = set(evidence.get("changed_files") or [])
    if _TESTS_PASS.search(body):
        if not runs:
            claims.append("claims tests pass but no validation ran")
        elif not runs[-1].get("ok"):
            claims.append("claims tests pass but the last validation failed")
    if _ADDED_TEST.search(body) and not any(
        "test" in f.lower() or "spec" in f.lower() for f in changed
    ):
        claims.append("claims a test was added but no test file changed")
    for sentence in re.split(r"(?<=[.!?\n])\s+", body):
        if not _CHANGED_VERB.search(sentence):
            continue
        for f in _FILE.findall(sentence):
            name = f.rsplit("/", 1)[-1]
            if changed and not any(
                c == f or c.endswith("/" + f) or c.endswith(name) for c in changed
            ):
                claims.append(f"claims {f} was changed but it was not")
            elif not changed:
                claims.append(f"claims {f} was changed but no file changed")
    return sorted(set(claims))
