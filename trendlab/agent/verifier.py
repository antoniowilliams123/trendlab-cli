"""Verifier (cheap-model spec §3.2): a fresh-context review of the diff before a run that
changed files is reported as done.

The verifier sees the task, the unified diff, the latest validation result and the plan, and
answers one fixed-schema question. It never sees the author's conversation, so it cannot be
talked into agreeing. ``fix`` hands findings back to the author for one more round;
``fail`` stops the run (required mode) or is reported (advisory mode).
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

MAX_DIFF_CHARS = 60_000
MAX_LOG_CHARS = 4_000

PROMPT = """You are the verifier for an autonomous coding agent. Another model made the change
below. Decide whether it should be surfaced to the user as done.

Judge only what is in front of you: does the diff do what the task asked, is it minimal and
safe, does the validation evidence support it, and (for a bug fix) is there a test that would
catch the bug again. Ignore length, formatting and writing style — a longer diff or a longer
explanation is not a better one, and a terse correct change must pass.

Score the rubric 0–2 each: correctness (does what was asked), minimality (nothing beyond the
task), safety (no data loss, no secrets, no new deps), tests (a test proves it).

Answer with ONE JSON object and nothing else:
{{"verdict": "pass" | "fix" | "fail",
 "confidence": 0.0-1.0 (how sure you are the change is correct),
 "rubric": {{"correctness": 0, "minimality": 0, "safety": 0, "tests": 0}},
 "findings": [{{"file": "path", "line": 0, "issue": "what is wrong",
               "severity": "high" | "med" | "low"}}],
 "regression_test": "present" | "missing" | "not_applicable"}}

verdict rules: "pass" = ship it; "fix" = concrete findings the author can address in one
round; "fail" = the change is wrong or unsafe and must not be surfaced.

## Task
{task}

## Plan
{plan}

## Diff
{diff}

## Latest validation
{validation}
"""

_JSON = re.compile(r"\{.*\}", re.S)


@dataclass
class VerifierVerdict:
    verdict: str  # pass | fix | fail
    findings: list[dict[str, Any]] = field(default_factory=list)
    regression_test: str = "not_applicable"
    raw: str = ""
    model: str = ""
    rubric: dict[str, int] = field(default_factory=dict)
    confidence: float | None = None  # verifier's P(change is correct); calibration (Brier/ECE)

    def to_json(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "findings": self.findings,
            "regression_test": self.regression_test,
            "model": self.model,
            "rubric": self.rubric,
            "confidence": self.confidence,
        }

    def feedback(self) -> str:
        """The message handed back to the author for a 'fix' round."""
        lines = [
            "An independent review of your change found issues. Address each one, run the"
            " validation again, then give the final answer:"
        ]
        for f in self.findings[:10]:
            where = f.get("file") or ""
            if f.get("line"):
                where += f":{f['line']}"
            lines.append(f"- [{f.get('severity', 'med')}] {where}: {f.get('issue', '')}".rstrip())
        if self.regression_test == "missing":
            lines.append(
                "- a regression test is missing: add a test that fails without the fix and "
                "passes with it (or say why that is not applicable)"
            )
        return "\n".join(lines)

    def footer(self) -> str:
        n = len(self.findings)
        head = {"pass": "verified", "fix": "verifier asked for fixes", "fail": "verifier rejected"}
        text = head.get(self.verdict, self.verdict)
        if n:
            text += f" ({n} finding{'s' if n != 1 else ''})"
        return text


def parse_verdict(text: str) -> VerifierVerdict | None:
    m = _JSON.search(text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    verdict = str(obj.get("verdict") or "").lower().strip()
    if verdict not in {"pass", "fix", "fail"}:
        return None
    findings = []
    for f in obj.get("findings") or []:
        if isinstance(f, dict) and f.get("issue"):
            findings.append(
                {
                    "file": str(f.get("file") or ""),
                    "line": int(f.get("line") or 0) if str(f.get("line") or "0").isdigit() else 0,
                    "issue": str(f["issue"])[:400],
                    "severity": str(f.get("severity") or "med").lower()[:4],
                }
            )
    reg = str(obj.get("regression_test") or "not_applicable").lower()
    if reg not in {"present", "missing", "not_applicable"}:
        reg = "not_applicable"
    rubric: dict[str, int] = {}
    for k in ("correctness", "minimality", "safety", "tests"):
        v = (obj.get("rubric") or {}).get(k)
        if isinstance(v, int | float):
            rubric[k] = max(0, min(2, int(v)))
    conf = obj.get("confidence")
    confidence = None
    if isinstance(conf, int | float) and 0 <= float(conf) <= 1:
        confidence = round(float(conf), 3)
    return VerifierVerdict(
        verdict=verdict,
        findings=findings,
        regression_test=reg,
        raw=text,
        rubric=rubric,
        confidence=confidence,
    )


def build_messages(
    *, task: str, diff: str, validation: dict[str, Any] | None, plan: str
) -> list[dict[str, Any]]:
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[: MAX_DIFF_CHARS // 2] + "\n… [diff elided] …\n" + diff[-MAX_DIFF_CHARS // 2 :]
    if validation:
        tail = str(validation.get("tail") or "")[-MAX_LOG_CHARS:]
        val = (
            f"command: {validation.get('command')}\nok: {validation.get('ok')} "
            f"(exit {validation.get('exit_code')})\n{tail}"
        )
    else:
        val = "(no validation was run)"
    content = PROMPT.format(
        task=task.strip()[:6000] or "(no task text)",
        plan=plan.strip() or "(no plan)",
        diff=diff or "(empty diff)",
        validation=val,
    )
    return [{"role": "user", "content": content}]


Caller = Callable[[list[dict[str, Any]]], Awaitable[tuple[str, str]]]


async def verify(
    call: Caller, *, task: str, diff: str, validation: dict[str, Any] | None, plan: str
) -> VerifierVerdict | None:
    """Ask the verifier model once; ``None`` when the answer is unusable (= unavailable)."""
    from trendlab.providers.structured_json import ask_json

    messages = build_messages(task=task, diff=diff, validation=validation, plan=plan)
    used: list[str] = []

    async def text_only(msgs):
        text, model = await call(msgs)
        used.append(model)
        return text

    verdict, _attempts = await ask_json(text_only, messages, parse_verdict)
    if verdict is not None:
        verdict.model = used[-1] if used else ""
    return verdict
