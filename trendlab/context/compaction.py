"""Structured context compaction (spec §20). Critical facts are kept as structured state, so the
free-form summary is a convenience, not the only record."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from trendlab.ui.attachments import text_of

SUMMARY_FIELDS = (
    "OBJECTIVE",
    "CURRENT STATUS",
    "DECISIONS",
    "COMPLETED WORK",
    "FILES MODIFIED",
    "IMPORTANT FINDINGS",
    "FAILED APPROACHES",
    "VALIDATION RESULTS",
    "USER DECISIONS",
    "UNRESOLVED QUESTIONS",
    "NEXT ACTION",
)


class CompactionRecord(BaseModel):
    summary: str
    structured: dict[str, Any] = Field(default_factory=dict)
    messages_compacted: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    source: str = "model"  # model | deterministic


def compaction_prompt(
    messages: list[dict[str, Any]], structured: dict[str, Any]
) -> list[dict[str, Any]]:
    transcript = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        content = text_of(m.get("content"))
        if m.get("tool_calls"):
            content += " " + "; ".join(
                f"CALL {c['function']['name']}({c['function']['arguments'][:200]})"
                for c in m["tool_calls"]
            )
        transcript.append(
            f"[{role}{(' ' + m['name']) if m.get('name') else ''}] {str(content)[:1500]}"
        )
    text = "\n".join(transcript)
    sections = "\n".join(f"{f}:" for f in SUMMARY_FIELDS)
    return [
        {
            "role": "system",
            "content": "You compact an agent's working transcript into a structured "
            "summary. Be concrete: file paths, commands, exact error text, "
            "numbers. Never invent results. Output ONLY the sections.",
        },
        {
            "role": "user",
            "content": f"Known structured state:\n{json.dumps(structured, indent=1)[:4000]}\n\n"
            f"Transcript:\n{text[:60000]}\n\nWrite these sections:\n{sections}",
        },
    ]


_HARNESS = (
    "Before finishing",
    "A step plan was prepared",
    "Step ",
    "Skill '",
    "An independent review of your change",
    "Time check:",
    "Before you finish, rewrite",
)
_SIGNAL = re.compile(
    r"(error|exception|traceback|assert|failed|failure|denied|\d+ (passed|failed))", re.I
)


def deterministic_summary(messages: list[dict[str, Any]], structured: dict[str, Any]) -> str:
    """Fallback when no summarizer model is available: keep, verbatim and bounded, what the
    summarizer would — the user's words (decisions), the agent's own notes (findings, failed
    approaches, next steps) and the error / test-result lines from tool output."""
    users = [
        text_of(m.get("content"))
        for m in messages
        if m.get("role") == "user" and not text_of(m.get("content")).startswith(_HARNESS)
    ]
    notes = [
        " ".join(text_of(m.get("content")).split())
        for m in messages
        if m.get("role") == "assistant" and len(text_of(m.get("content")).split()) >= 4
    ]
    signals: list[str] = []
    for m in messages:
        if m.get("role") != "tool" or not isinstance(m.get("content"), str):
            continue
        for line in m["content"].splitlines():
            line = line.strip()
            if line and _SIGNAL.search(line) and line[:200] not in signals:
                signals.append(line[:200])
    lines = [
        f"OBJECTIVE: {users[0][:500] if users else '(unknown)'}",
        "USER DECISIONS: " + (" | ".join(u[:300] for u in users[1:13]) or "none"),
        "AGENT NOTES (findings, failed approaches, next steps): "
        + (" | ".join(n[:300] for n in notes[-15:]) or "none"),
        "ERRORS AND TEST RESULTS SEEN: " + (" | ".join(signals[-10:]) or "none"),
        f"FILES MODIFIED: {', '.join(structured.get('changed_files', [])) or 'none'}",
        f"VALIDATION RESULTS: {json.dumps(structured.get('validation_runs', [])[-3:])}",
        f"CURRENT PLAN: {structured.get('plan', '(none)')}",
    ]
    return "\n".join(lines)[:8000]
