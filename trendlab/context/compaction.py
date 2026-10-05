"""Structured context compaction (spec §20). Critical facts are kept as structured state, so the
free-form summary is a convenience, not the only record."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

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
        content = m.get("content") or ""
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


def deterministic_summary(messages: list[dict[str, Any]], structured: dict[str, Any]) -> str:
    """Fallback when no summarizer model is available: pull facts straight from the transcript."""
    user_msgs = [
        m["content"]
        for m in messages
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    failures = [
        m["content"][:200]
        for m in tool_msgs
        if isinstance(m.get("content"), str)
        and m["content"]
        .lower()
        .startswith(("denied", "not executed", "patch failed", "conflict", "tool error"))
    ]
    lines = [
        f"OBJECTIVE: {user_msgs[0][:500] if user_msgs else '(unknown)'}",
        f"CURRENT STATUS: {len(messages)} messages compacted; see structured state",
        f"FILES MODIFIED: {', '.join(structured.get('changed_files', [])) or 'none'}",
        f"VALIDATION RESULTS: {json.dumps(structured.get('validation_runs', [])[-3:])}",
        f"FAILED APPROACHES: {' | '.join(failures[:5]) or 'none recorded'}",
        f"CURRENT PLAN: {structured.get('plan', '(none)')}",
        "NEXT ACTION: continue the active plan task",
    ]
    return "\n".join(lines)
