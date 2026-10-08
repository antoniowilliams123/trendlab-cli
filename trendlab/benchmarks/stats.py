"""Statistics for the benchmark (uplift U1): intervals, paired tests, pass@k, flakiness.

Pure standard library so the harness stays dependency-free. Small-sample honesty is the point:
a suite of 10–40 tasks cannot distinguish two configurations that differ by one task, and the
numbers here say so instead of hiding it.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from typing import Any


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95 % Wilson score interval for a proportion k/n (0, 0 when n == 0)."""
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3))


def bootstrap_ci(
    values: Sequence[float], *, n_boot: int = 2000, seed: int = 0, alpha: float = 0.05
) -> tuple[float, float]:
    """Percentile bootstrap interval for the mean."""
    vals = list(values)
    if not vals:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(vals)
    means = sorted(sum(rng.choice(vals) for _ in range(n)) / n for _ in range(n_boot))
    lo = means[int(alpha / 2 * n_boot)]
    hi = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return (round(lo, 4), round(hi, 4))


def exact_sign_test(wins: int, losses: int) -> float:
    """Two-sided exact binomial (sign) test p-value for paired binary outcomes; ties ignored.
    With wins + losses == 0 there is no evidence either way: p = 1."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / 2**n
    return round(min(1.0, 2 * tail), 4)


def paired_outcomes(
    a_rows: list[dict[str, Any]], b_rows: list[dict[str, Any]], metric: str
) -> dict[str, Any]:
    """Pair rows by task (majority vote across repeat runs) and count wins/losses for B over A."""

    def vote(rows: list[dict[str, Any]]) -> dict[str, bool]:
        by: dict[str, list[bool]] = {}
        for r in rows:
            if r.get("skipped"):
                continue
            by.setdefault(r["task"], []).append(bool(r.get(metric)))
        return {t: sum(v) * 2 >= len(v) for t, v in by.items()}

    a, b = vote(a_rows), vote(b_rows)
    common = sorted(set(a) & set(b))
    wins = sum(1 for t in common if b[t] and not a[t])
    losses = sum(1 for t in common if a[t] and not b[t])
    return {
        "tasks": len(common),
        "b_wins": wins,
        "b_losses": losses,
        "ties": len(common) - wins - losses,
        "p_value": exact_sign_test(wins, losses),
        "tasks_only_b_passes": [t for t in common if b[t] and not a[t]],
        "tasks_only_a_passes": [t for t in common if a[t] and not b[t]],
    }


def pass_at_k(rows: list[dict[str, Any]], metric: str = "passes") -> dict[str, float]:
    """Across repeat runs per task: pass@1 (mean over runs) and pass@k (any run passed)."""
    by: dict[str, list[bool]] = {}
    for i, r in enumerate(rows):
        if not r.get("skipped"):
            by.setdefault(str(r.get("task") or f"row{i}"), []).append(bool(r.get(metric)))
    if not by:
        return {"pass_at_1": 0.0, "pass_at_k": 0.0, "k": 0, "flaky_tasks": 0}
    k = max(len(v) for v in by.values())
    p1 = sum(sum(v) / len(v) for v in by.values()) / len(by)
    pk = sum(1 for v in by.values() if any(v)) / len(by)
    flaky = sum(1 for v in by.values() if len(v) > 1 and any(v) and not all(v))
    return {"pass_at_1": round(p1, 3), "pass_at_k": round(pk, 3), "k": k, "flaky_tasks": flaky}


def gate(
    paired: dict[str, Any], cost_ratio: float, *, max_p: float = 0.05, max_cost_ratio: float = 1.5
) -> dict[str, Any]:
    """Release gate (U1): B may not be significantly worse than A on the metric, and may not cost
    more than ``max_cost_ratio`` × A. Returns {ok, reasons}."""
    reasons = []
    if paired["b_losses"] > paired["b_wins"] and paired["p_value"] <= max_p:
        reasons.append(
            f"B loses {paired['b_losses']} tasks vs wins {paired['b_wins']} (p={paired['p_value']})"
        )
    if cost_ratio > max_cost_ratio:
        reasons.append(f"B costs {cost_ratio:.2f}× A (limit {max_cost_ratio}×)")
    return {"ok": not reasons, "reasons": reasons}


def classifier_metrics(pairs: list[tuple[bool, bool]]) -> dict[str, Any]:
    """Binary classifier scores. ``pairs`` = (predicted_positive, actually_positive).

    For the verifier, *positive* means "this change is wrong": predicted positive = the
    verifier flagged it (fix/fail), actually positive = the hidden test failed."""
    tp = sum(1 for p, a in pairs if p and a)
    fp = sum(1 for p, a in pairs if p and not a)
    fn = sum(1 for p, a in pairs if not p and a)
    tn = sum(1 for p, a in pairs if not p and not a)

    def ratio(x: int, y: int) -> float | None:
        return round(x / y, 3) if y else None

    precision = ratio(tp, tp + fp)
    recall = ratio(tp, tp + fn)
    f1 = (
        round(2 * precision * recall / (precision + recall), 3)
        if precision is not None and recall is not None and (precision + recall) > 0
        else None
    )
    return {
        "n": len(pairs),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_positive_rate": ratio(fp, fp + tn),
        "false_negative_rate": ratio(fn, fn + tp),
        "accuracy": ratio(tp + tn, len(pairs)),
    }


def calibration(probs_and_truth: list[tuple[float, bool]], bins: int = 5) -> dict[str, Any]:
    """Brier score and expected calibration error for stated confidences (P(correct))."""
    pts = [(float(p), bool(t)) for p, t in probs_and_truth if p is not None]
    if not pts:
        return {"n": 0, "brier": None, "ece": None, "bins": []}
    brier = sum((p - (1.0 if t else 0.0)) ** 2 for p, t in pts) / len(pts)
    table = []
    ece = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        inside = [(p, t) for p, t in pts if (lo <= p < hi) or (b == bins - 1 and p == 1.0)]
        if not inside:
            continue
        conf = sum(p for p, _ in inside) / len(inside)
        acc = sum(1 for _, t in inside if t) / len(inside)
        ece += len(inside) / len(pts) * abs(conf - acc)
        table.append(
            {
                "bin": f"{lo:.1f}-{hi:.1f}",
                "n": len(inside),
                "confidence": round(conf, 3),
                "accuracy": round(acc, 3),
            }
        )
    return {"n": len(pts), "brier": round(brier, 4), "ece": round(ece, 4), "bins": table}


def cohen_kappa(a: list[Any], b: list[Any]) -> float | None:
    """Agreement between two raters beyond chance (inter-rater / judge agreement)."""
    pairs = [(x, y) for x, y in zip(a, b, strict=False) if x is not None and y is not None]
    if not pairs:
        return None
    n = len(pairs)
    labels = sorted({x for x, _ in pairs} | {y for _, y in pairs}, key=str)
    po = sum(1 for x, y in pairs if x == y) / n
    pe = sum(
        (sum(1 for x, _ in pairs if x == lab) / n) * (sum(1 for _, y in pairs if y == lab) / n)
        for lab in labels
    )
    if pe >= 1.0:
        return 1.0 if po == 1.0 else 0.0
    return round((po - pe) / (1 - pe), 3)


def capability_map(
    rows: list[dict[str, Any]], key: str = "defect_kind"
) -> dict[str, dict[str, Any]]:
    """Pass rate per group (defect kind, language, tier): where the model is strong or weak
    — the jagged edge of its capability, measured instead of guessed."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r.get("skipped"):
            continue
        groups.setdefault(str(r.get(key) or "?"), []).append(r)
    out = {}
    for g, rs in sorted(groups.items()):
        k = sum(1 for r in rs if r.get("passes"))
        out[g] = {
            "n": len(rs),
            "passes": round(k / len(rs), 3),
            "ci95": wilson_interval(k, len(rs)),
        }
    return out
