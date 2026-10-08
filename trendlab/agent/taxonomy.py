"""One failure taxonomy for the whole harness (uplift U8).

Every failed run, every failed bench row and every meta-loop card carries a code from here, so
"why do runs fail" has one vocabulary: the runtime's stop reasons, the recovery classes, the
verifier, the loop detector and the benchmark's hidden tests all map onto these codes.

Each code also carries what failure-mode analysis needs: a severity (1-10, how bad it is when it
reaches the user) and a detection score (1-10, 10 = the harness cannot catch it before the user
sees it). Occurrence comes from data, so ``fmea`` ranks codes by risk priority S x O x D.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Code:
    code: str
    category: str
    summary: str
    severity: int
    detection: int
    causes: tuple[str, ...]
    remedies: tuple[str, ...]
    suspects: tuple[str, ...] = ()


_C = [
    # budgets
    Code(
        "BUDGET_ITERATIONS",
        "budget",
        "ran out of iterations",
        4,
        2,
        ("task larger than the iteration cap", "model looping without a loop signal"),
        ("raise [limits] max_iterations for this task", "check the trace for repeated reads"),
        ("trendlab/agent/runtime.py",),
    ),
    Code(
        "BUDGET_COST",
        "budget",
        "hit the cost cap",
        4,
        1,
        ("expensive model on a long task", "verifier or planner on a small task"),
        ("route small tasks to the cheap path", "raise [limits] max_cost_usd deliberately"),
        ("trendlab/agent/router.py", "trendlab/agent/runtime.py"),
    ),
    Code(
        "BUDGET_CALLS",
        "budget",
        "hit the model-call cap",
        4,
        2,
        ("many short turns",),
        ("raise [limits] max_model_calls",),
        ("trendlab/agent/runtime.py",),
    ),
    Code(
        "BUDGET_TIME",
        "budget",
        "hit the wall-clock cap",
        4,
        2,
        ("slow provider", "long test command"),
        ("check provider latency in the trace", "narrow the test command"),
        ("trendlab/agent/runtime.py",),
    ),
    # provider / context
    Code(
        "PROVIDER_TIMEOUT",
        "provider",
        "model call timed out",
        3,
        1,
        ("provider overloaded", "very long prompt"),
        ("retry later or set a fallback model",),
        ("trendlab/providers/",),
    ),
    Code(
        "PROVIDER_RATE_LIMIT",
        "provider",
        "rate limited by the provider",
        3,
        1,
        ("too many parallel runs",),
        ("lower concurrency or add a fallback model",),
        ("trendlab/providers/",),
    ),
    Code(
        "PROVIDER_ERROR",
        "provider",
        "provider returned an error",
        3,
        1,
        ("provider outage", "bad request shape"),
        ("check the provider status; set a fallback",),
        ("trendlab/providers/",),
    ),
    Code(
        "PROVIDER_AUTH",
        "provider",
        "provider rejected the key",
        5,
        1,
        ("missing or expired key",),
        ("run `trendlab doctor` and fix the key",),
        ("trendlab/providers/",),
    ),
    Code(
        "CONTEXT_OVERFLOW",
        "context",
        "context overflowed even after compaction",
        5,
        1,
        ("huge tool output kept in context", "very long session"),
        ("start a fresh session", "tier the tool output that blew the window"),
        ("trendlab/context/", "trendlab/tools/views/"),
    ),
    # agent behaviour
    Code(
        "NO_PROGRESS",
        "behaviour",
        "the agent repeated itself without progress",
        5,
        2,
        ("same failing command retried", "edit/undo cycle"),
        ("give the agent the missing fact", "escalate to a stronger model"),
        ("trendlab/agent/loop_detector.py", "trendlab/agent/recovery.py"),
    ),
    Code(
        "INVARIANT_BROKEN",
        "harness",
        "a runtime invariant failed",
        7,
        1,
        ("harness bug", "state corrupted by a tool"),
        ("file a harness card with the session id",),
        ("trendlab/agent/invariants.py", "trendlab/agent/runtime.py"),
    ),
    Code(
        "PLAN_REJECTED",
        "user",
        "the user rejected the plan",
        2,
        1,
        ("plan did not match intent",),
        ("restate the goal with the constraint that was missed",),
        ("trendlab/agent/planner.py",),
    ),
    Code("CANCELED", "user", "canceled by the user", 1, 1, ("user stopped the run",), (), ()),
    Code(
        "VERIFIER_REJECTED",
        "quality",
        "the verifier rejected the change",
        6,
        2,
        ("wrong fix", "fix broke something else", "verifier false alarm"),
        ("read the verifier findings", "check verifier precision in bench --scorecard"),
        ("trendlab/agent/verifier.py",),
    ),
    Code(
        "TOOL_BROKEN",
        "tool",
        "a tool kept failing (circuit opened)",
        5,
        1,
        ("missing binary", "environment problem"),
        ("fix the environment; see `trendlab tools`",),
        ("trendlab/tools/health.py", "trendlab/tools/runtime.py"),
    ),
    # outcomes only the benchmark can see (hidden tests, forbid checks)
    Code(
        "WRONG_FIX",
        "quality",
        "finished, but the hidden tests fail",
        8,
        8,
        ("symptom patched, not the cause", "misread the task"),
        ("raise verification for this route", "add the failing case to the canary"),
        ("trendlab/agent/verifier.py", "trendlab/agent/router.py"),
    ),
    Code(
        "COLLATERAL_DAMAGE",
        "quality",
        "fixed the task but broke other tests",
        9,
        7,
        ("edit outside the task's scope", "shared state changed"),
        ("tighten the scope budget", "require the full test run before surfacing"),
        ("trendlab/agent/scope.py", "trendlab/agent/runtime.py"),
    ),
    Code(
        "MISSING_REGRESSION_TEST",
        "quality",
        "fixed without a regression test",
        4,
        3,
        ("model skipped the test step",),
        ("keep the regression gate on for fixes",),
        ("trendlab/agent/evaluator.py",),
    ),
    Code(
        "WRONG_ANSWER",
        "quality",
        "answered a question wrongly",
        7,
        8,
        ("did not read the right file", "guessed"),
        ("turn retrieval hints on", "require a file citation for answers"),
        ("trendlab/context/retrieval.py",),
    ),
    Code(
        "UNSAFE_ACTION",
        "safety",
        "did something the task forbade",
        10,
        4,
        ("followed injected instructions", "destructive command"),
        ("check the injection scanner and taint gating", "tighten permissions"),
        ("trendlab/security/injection.py", "trendlab/tools/runtime.py"),
    ),
    Code(
        "CRASHED",
        "harness",
        "the run raised an exception",
        8,
        1,
        ("harness bug",),
        ("file a harness card with the traceback",),
        ("trendlab/agent/runtime.py",),
    ),
    Code(
        "UNKNOWN",
        "unknown",
        "failure not yet classified",
        5,
        5,
        ("new failure path",),
        ("add a pattern to trendlab/agent/taxonomy.py",),
        ("trendlab/agent/taxonomy.py",),
    ),
]
CODES: dict[str, Code] = {c.code: c for c in _C}

# Stop-reason text → code. Order matters: the first match wins.
_STOP = [
    (r"^max_iterations", "BUDGET_ITERATIONS"),
    (r"^cost limit", "BUDGET_COST"),
    (r"^max_model_calls", "BUDGET_CALLS"),
    (r"^wall-clock", "BUDGET_TIME"),
    (r"^cancel", "CANCELED"),
    (r"^invariant violated", "INVARIANT_BROKEN"),
    (r"^context overflow|^CONTEXT_OVERFLOW", "CONTEXT_OVERFLOW"),
    (r"^MODEL_TIMEOUT|^COMMAND_TIMEOUT", "PROVIDER_TIMEOUT"),
    (r"^RATE_LIMIT", "PROVIDER_RATE_LIMIT"),
    (r"^AUTH_FAILURE", "PROVIDER_AUTH"),
    (r"^PROVIDER_FAILURE|^MALFORMED_TOOL_CALL", "PROVIDER_ERROR"),
    (r"^plan rejected", "PLAN_REJECTED"),
    (r"^NO_PROGRESS", "NO_PROGRESS"),
    (r"^verifier rejected", "VERIFIER_REJECTED"),
    (r"circuit (is )?open", "TOOL_BROKEN"),
    (r"^(crash|error):|Traceback", "CRASHED"),
]


def classify_stop(status: str, stop_reason: str | None) -> str | None:
    """Code for a run's outcome; None for a completed run."""
    if status == "COMPLETED":
        return None
    if status == "CANCELED":
        return "CANCELED"
    text = (stop_reason or "").strip()
    for pattern, code in _STOP:
        if re.search(pattern, text):
            return code
    return "UNKNOWN"


def classify_row(row: dict[str, Any]) -> str | None:
    """Code for a benchmark row; None when the row is a clean success.

    Safety outranks everything; then the run's own stop reason; then what only the hidden tests
    can see (wrong fix, collateral damage, missing regression test, wrong answer).
    """
    if row.get("safe") is False:
        return "UNSAFE_ACTION"
    if row.get("status") == "CRASHED":
        return "CRASHED"
    status = str(row.get("status") or "")
    if status and status != "COMPLETED":
        return classify_stop(status, row.get("stop_reason"))
    if row.get("answered") is False:
        return "WRONG_ANSWER"
    if row.get("passes") is False:
        return "WRONG_FIX"
    if row.get("no_collateral") is False:
        return "COLLATERAL_DAMAGE"
    if row.get("regression_added") is False and row.get("answered") is None:
        return "MISSING_REGRESSION_TEST"
    return None


def by_code(codes: list[str | None]) -> dict[str, int]:
    return dict(Counter(c for c in codes if c).most_common())


def fmea(counts: dict[str, int], total_runs: int) -> list[dict[str, Any]]:
    """Failure-mode table: severity x occurrence x detection, highest risk first.

    Occurrence maps the observed rate to 1-10 (any occurrence is at least 2; 50%+ is 10).
    """
    rows = []
    for code, n in counts.items():
        c = CODES.get(code, CODES["UNKNOWN"])
        rate = n / total_runs if total_runs else 0.0
        occurrence = 1 if n == 0 else min(10, max(2, round(rate * 20)))
        rows.append(
            {
                "code": code,
                "category": c.category,
                "count": n,
                "rate": round(rate, 3),
                "severity": c.severity,
                "occurrence": occurrence,
                "detection": c.detection,
                "rpn": c.severity * occurrence * c.detection,
                "remedy": c.remedies[0] if c.remedies else "",
            }
        )
    rows.sort(key=lambda r: (-r["rpn"], -r["count"], r["code"]))
    return rows


# -- root-cause analysis over one session's events ---------------------------------------------

_SIGNAL_TYPES = {
    "tool.completed",
    "guard.fired",
    "loop.detected",
    "recovery.action",
    "verify.verdict",
    "invariant.violated",
    "tool.circuit_opened",
    "tool.postcondition_failed",
    "security.injection_suspected",
    "scope.checked",
    "budget.exceeded",
    "provider.retry",
    "provider.fallback",
    "approval.denied",
    "step.failed",
}


def _signal(ev: dict[str, Any]) -> str | None:
    t, d = ev.get("type", ""), ev.get("data") or {}
    if t not in _SIGNAL_TYPES:
        return None
    if t == "tool.completed":
        return None if d.get("ok", True) else f"{d.get('tool')} failed: {d.get('error', '')}"[:200]
    if t == "verify.verdict":
        return None if d.get("verdict") == "pass" else f"verifier said {d.get('verdict')}"
    if t == "scope.checked":
        return None if d.get("ok", True) else f"scope exceeded: {d.get('reason', '')}"[:200]
    if t == "guard.fired":
        return f"guard {d.get('guard')}"
    if t == "loop.detected":
        return f"loop on {d.get('tool')}: {d.get('reason', '')}"[:200]
    if t == "recovery.action":
        return f"recovery {d.get('failure')} → {d.get('action')}"
    if t == "invariant.violated":
        return f"invariant: {d.get('message', '')}"[:200]
    if t == "tool.circuit_opened":
        return f"circuit opened for {d.get('tool')}"
    if t == "security.injection_suspected":
        return f"injection suspected in {d.get('tool')}"
    if t == "approval.denied":
        return f"approval denied for {d.get('tool') or d.get('kind') or 'action'}"
    return t


def rca(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per run in the session: its code, the first warning sign, the chain of signals
    leading to the stop, and the remedies. Deterministic: no model call."""
    out: list[dict[str, Any]] = []
    chain: list[dict[str, Any]] = []
    for ev in events:
        t = ev.get("type", "")
        if t in {"run.completed", "run.failed", "run.canceled"}:
            d = ev.get("data") or {}
            status = {"run.completed": "COMPLETED", "run.canceled": "CANCELED"}.get(t, "FAILED")
            code = d.get("failure_code") or classify_stop(status, d.get("stop_reason"))
            entry: dict[str, Any] = {
                "ts": ev.get("ts"),
                "status": status,
                "code": code,
                "stop_reason": d.get("stop_reason"),
                "signals": [c["what"] for c in chain][-12:],
                "first_sign": chain[0]["what"] if chain else None,
            }
            if code:
                c = CODES.get(code, CODES["UNKNOWN"])
                entry.update(
                    summary=c.summary,
                    likely_causes=list(c.causes),
                    remedies=list(c.remedies),
                    suspects=list(c.suspects),
                )
            out.append(entry)
            chain = []
            continue
        what = _signal(ev)
        if what:
            chain.append({"ts": ev.get("ts"), "what": what})
    return out
