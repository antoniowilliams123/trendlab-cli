"""Completion evaluator (spec §25, §49, §50): evidence decides completion, not the model's prose."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trendlab.agent.tasks import Plan


@dataclass
class EvidenceSummary:
    changed_files: list[str]
    validation_runs: list[dict[str, Any]]
    plan: Plan

    @property
    def mutated(self) -> bool:
        return bool(self.changed_files)

    @property
    def last_validation(self) -> dict[str, Any] | None:
        return self.validation_runs[-1] if self.validation_runs else None

    @property
    def validated(self) -> bool:
        last = self.last_validation
        return bool(last and last.get("ok"))


@dataclass
class Verdict:
    accept: bool
    nudge: str | None = None
    reasons: list[str] = field(default_factory=list)


class CompletionEvaluator:
    """Decide whether a final (tool-free) model message may end the run."""

    def __init__(self, max_nudges: int = 2) -> None:
        self.max_nudges = max_nudges
        self.nudges = 0

    def evaluate(
        self, ev: EvidenceSummary, final_text: str, *, validation_available: bool
    ) -> Verdict:
        reasons: list[str] = []
        if ev.mutated and validation_available and not ev.validation_runs:
            reasons.append("files were changed but no validation (tests/lint) was run afterwards")
        elif ev.mutated and ev.last_validation and not ev.last_validation.get("ok"):
            reasons.append("the most recent validation run failed")
        if ev.plan.tasks and ev.plan.open and not _acknowledges_incomplete(final_text):
            open_titles = ", ".join(t.title for t in ev.plan.open[:4])
            reasons.append(f"plan tasks are still open: {open_titles}")
        if not reasons or self.nudges >= self.max_nudges:
            return Verdict(accept=True, reasons=reasons)
        self.nudges += 1
        nudge = (
            "Before finishing, address the following: "
            + "; ".join(reasons)
            + ". Either run the validation / finish the open tasks with tools, or state explicitly "
            "why that is not possible and mark the tasks accordingly."
        )
        return Verdict(accept=False, nudge=nudge, reasons=reasons)


def _acknowledges_incomplete(text: str) -> bool:
    lowered = text.lower()
    return any(
        k in lowered
        for k in (
            "could not",
            "cannot",
            "unable",
            "blocked",
            "not possible",
            "denied",
            "incomplete",
            "remaining",
            "stopped",
        )
    )


def final_report(
    ev: EvidenceSummary,
    text: str,
    *,
    cost_usd: float,
    elapsed_s: float,
    status: str,
    model_calls: int,
) -> str:
    """Render the final response contract (spec §50)."""
    glyph = {"COMPLETED": "✓ Completed", "FAILED": "✗ Failed", "CANCELED": "■ Canceled"}.get(
        status, status
    )
    lines = [glyph, ""]
    if text.strip():
        lines += [text.strip(), ""]
    if ev.changed_files:
        lines.append("Changed")
        lines += [f"- {f}" for f in ev.changed_files]
        lines.append("")
    if ev.validation_runs:
        lines.append("Validation")
        for run in ev.validation_runs[-5:]:
            mark = "✓" if run.get("ok") else "✗"
            lines.append(f"- {mark} {run.get('command')}")
        lines.append("")
    elif ev.mutated:
        lines += ["Validation", "- none run", ""]
    if ev.plan.tasks:
        done = sum(1 for t in ev.plan.tasks if t.status.value == "completed")
        lines += ["Plan", f"- {done}/{len(ev.plan.tasks)} tasks completed", ""]
    lines += ["Cost", f"- ${cost_usd:.3f} · {model_calls} model calls · {elapsed_s:.0f}s"]
    return "\n".join(lines)
