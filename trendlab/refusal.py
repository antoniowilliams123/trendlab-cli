"""C15: a refusal with no attempt behind it (uplift: capability_unchecked_rate).

An answer that ends on "I don't have access to that" or "I cannot look that up" is a claim
about the harness, and the harness holds the evidence: the tool catalog. When the tools that
could settle the question were never tried, the claim is unsupported — the run stopped short
instead of reaching for a tool. The rate of those is ``capability_unchecked_rate``.

This module is the home of the rule and stays dependency-free (stdlib only), so evals and the
runtime can both import it; :mod:`trendlab.agent.capability` re-exports it for the agent side.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

#: Tools that look something up. A refusal is unsupported only while none of these was tried.
LOOKUP_TOOLS = frozenset(
    {
        "read_file",
        "list_directory",
        "glob",
        "search_text",
        "inspect_output",
        "web_fetch",
        "web_search",
        "shell",
        "background",
        "delegate",
        "git_status",
        "git_diff",
        "git_log",
    }
)

_REFUSAL = re.compile(
    # "I don't have access / no tool / any way"
    r"\b(?:i|we)\s+(?:do\s?n['’]?t|do\s+not|does\s?n['’]?t)\s+have\s+"
    r"(?:any\s+|the\s+|access\s+to\s+)?"
    r"(?:access|tools?\b|a\s+way|the\s+ability|permission|network|internet)"
    # "I have no access / no way to check"
    r"|\b(?:i|we)\s+(?:have|has)\s+no\s+(?:access|tools?|way|ability)"
    # "I can't check / cannot look it up / fetch it" (not "can't help": that is a policy refusal)
    r"|\b(?:i|we)\s+(?:can['’]?t|cannot|can\s+not|am\s+unable|are\s+unable|['’]m\s+unable|"
    r"['’]re\s+unable)\s+(?:to\s+)?"
    r"(?:access|check|look|browse|fetch|search|read|reach|connect|query|verify|retrieve|see|"
    r"open)"
    # "I am not able to reach it"
    r"|\b(?:i|we)\s+(?:am|are|['’]m|['’]re)\s+not\s+able\s+to\s+"
    r"(?:access|check|look|browse|fetch|search|read|reach|connect|query|verify|retrieve)"
    # "my tools do not include / allow / support …"
    r"|\b(?:my|our|the)\s+tools?\s+(?:do\s?n['’]?t|do\s+not)\s+"
    r"(?:include|allow|support|cover|let)"
    # "no tools are available to me", "there is no way to"
    r"|\bno\s+tools?\s+(?:is|are)?\s*(?:available|to\s+me|here)"
    r"|\bthere\s+(?:is|['’]s)\s+no\s+(?:tool|way)\b",
    re.I,
)


def detect_unattempted_refusal(tool_calls: Iterable[str] = (), final_message: str = "") -> bool:
    """True when the final message refuses a capability that no tool call tried to deliver.

    ``tool_calls`` names the tools the turn called, ``final_message`` is the candidate answer.
    No tool call plus a refusal phrase means the run stopped short.
    :func:`unattempted_refusals` is the form that also knows which tools were on offer.
    """
    tried = {str(t) for t in (tool_calls or ())}
    if tried & LOOKUP_TOOLS:
        return False
    return bool(_REFUSAL.search(final_message or ""))


def unattempted_refusals(
    text: str, *, tools: Iterable[str] = (), attempted: Iterable[str] = ()
) -> list[str]:
    """Claims that something is out of reach, where the run never tried a tool that could
    settle it. ``tools`` is the catalog on offer, ``attempted`` the tool names the run called.

    Empty when the reply does not refuse, when the catalog holds no lookup tool (the
    limitation is real), or when a lookup tool was tried (the refusal may then be grounded).
    """
    body = text or ""
    match = _REFUSAL.search(body)
    if not match:
        return []
    on_offer = [str(t) for t in tools]
    if not on_offer:
        return []
    usable = sorted(t for t in on_offer if t in LOOKUP_TOOLS)
    if not usable or set(map(str, attempted)) & LOOKUP_TOOLS:
        return []
    phrase = " ".join(match.group(0).split())
    return [
        f'claims it cannot answer ("{phrase}") but never tried {", ".join(usable)} — '
        "call one before concluding it is out of reach"
    ]


def is_unattempted_refusal(messages: list[dict[str, Any]], tools_used: list[str]) -> bool:
    """The transcript-shaped form used by the evals: does the last assistant message refuse
    something that ``tools_used`` never tried to deliver?"""
    text = ""
    for message in reversed(messages or []):
        if str(message.get("role")) == "assistant" and message.get("content"):
            text = str(message["content"])
            break
    return detect_unattempted_refusal(tools_used, text)


@dataclass
class UnattemptedRefusalEvent:
    """One refusal that reached the user without a tool call behind it (C15)."""

    session_id: str
    task_id: str
    reason: str

    def to_telemetry(self) -> dict[str, str]:
        """The payload written to the event log, through redaction like every other event."""
        return {"session_id": self.session_id, "task_id": self.task_id, "reason": self.reason}
