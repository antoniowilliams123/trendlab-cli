"""Agent-side name for rule C15. The rule itself lives in :mod:`trendlab.refusal`.

That module is dependency-free, so the evaluator and the runtime import it through here
without dragging the rest of the agent package into anything that only needs the rule.
"""

from __future__ import annotations

from trendlab.refusal import (
    LOOKUP_TOOLS,
    UnattemptedRefusalEvent,
    detect_unattempted_refusal,
    is_unattempted_refusal,
    unattempted_refusals,
)

__all__ = [
    "LOOKUP_TOOLS",
    "UnattemptedRefusalEvent",
    "detect_unattempted_refusal",
    "is_unattempted_refusal",
    "unattempted_refusals",
]
