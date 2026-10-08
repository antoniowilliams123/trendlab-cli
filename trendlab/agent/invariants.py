"""Runtime invariants (uplift: invariant checking / runtime assertions).

Conditions that must hold after every iteration of the agent loop. A violation is a harness bug
or an escaped boundary, never a model mistake, so it is reported loudly (`invariant.violated`)
and, for the safety ones, stops the run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def check(agent: Any) -> list[tuple[str, bool]]:
    """Returns [(message, fatal)] for every invariant that does not hold."""
    out: list[tuple[str, bool]] = []
    root = Path(agent.tools.ctx.project_root).resolve()
    for rel in list(agent.tools.changed_files):
        p = (root / rel).resolve()
        try:
            p.relative_to(root)
        except ValueError:
            out.append((f"a changed file is outside the project: {rel}", True))
    active = [t for t in agent.plan.tasks if str(getattr(t.status, "value", t.status)) == "active"]
    if len(active) > 1:
        out.append((f"{len(active)} plan tasks are active at once", False))
    costs = agent.costs
    total = costs.total_usd
    if total < getattr(agent, "_last_cost", 0.0) - 1e-9:
        out.append(("session cost went down", False))
    agent._last_cost = total
    if any(r.cost_usd < 0 or r.input_tokens < 0 or r.output_tokens < 0 for r in costs.records):
        out.append(("a model call recorded negative cost or tokens", False))
    if agent.iterations > agent.limits.max_iterations + 1:
        out.append(("iteration limit exceeded", True))
    limit = agent.limits.max_cost_usd
    if limit is not None and total > limit * 1.5:
        out.append((f"spend {total:.4f} is far beyond the budget {limit:.4f}", True))
    return out
