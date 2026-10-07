"""Project memory (spec §92.1): durable facts a project teaches the agent, kept across sessions.

``<project>/.trendlab/memory.md`` holds one fact per line. It is loaded into the system prompt at
session start and after every change, and extended at the end of noteworthy runs from the run's
own evidence: how tests are run, build quirks, conventions the person insisted on, pitfalls hit.
Task-specific chatter is deliberately not stored. ``/memory`` shows and edits it; ``/remember``
adds a fact by hand.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

HEADER = (
    "# TrendLab project memory\n\nOne durable fact per line. Edit freely; /memory in TrendLab.\n\n"
)
_LINE = re.compile(r"^\s*[-*]\s*(?:\[(\d{4}-\d{2}-\d{2})\]\s*)?(.+?)\s*$")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class ProjectMemory:
    def __init__(
        self,
        project_root: Path,
        *,
        path: Path | None = None,
        max_entries: int = 60,
        max_prompt_chars: int = 6000,
    ) -> None:
        self.path = path or (project_root / ".trendlab" / "memory.md")
        self.max_entries = max_entries
        self.max_prompt_chars = max_prompt_chars
        self.entries: list[tuple[str, str]] = []  # (date, fact)
        self.load()

    # -- storage -----------------------------------------------------------------------------------
    def load(self) -> list[str]:
        self.entries = []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        for line in text.splitlines():
            m = _LINE.match(line)
            if m and not line.lstrip().startswith("#"):
                self.entries.append((m.group(1) or "", m.group(2)))
        return self.facts

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            body = "".join(
                f"- [{d}] {f}\n" if d else f"- {f}\n" for d, f in self.entries[-self.max_entries :]
            )
            self.path.write_text(HEADER + body, encoding="utf-8")
        except OSError:
            pass

    @property
    def facts(self) -> list[str]:
        return [f for _, f in self.entries]

    def add(self, fact: str, *, when: str | None = None) -> bool:
        """Add one fact; False when an equivalent one is already stored."""
        fact = " ".join(fact.strip().lstrip("-*• ").split())
        if len(fact) < 8:
            return False
        key = _norm(fact)
        if any(_norm(f) == key for f in self.facts):
            return False
        self.entries.append((when or date.today().isoformat(), fact[:300]))
        self.entries = self.entries[-self.max_entries :]
        self.save()
        return True

    def add_many(self, facts: list[str]) -> list[str]:
        return [f for f in facts if self.add(f)]

    def forget(self, index: int) -> str | None:
        """Remove the 1-based entry; returns the removed fact."""
        if 1 <= index <= len(self.entries):
            _, fact = self.entries.pop(index - 1)
            self.save()
            return fact
        return None

    def clear(self) -> int:
        n = len(self.entries)
        self.entries = []
        self.save()
        return n

    # -- prompt ------------------------------------------------------------------------------------
    def render_for_prompt(self) -> str:
        """Newest facts that fit the budget, as a system-prompt section (empty when none)."""
        if not self.entries:
            return ""
        lines: list[str] = []
        used = 0
        for _d, fact in reversed(self.entries):
            if used + len(fact) + 3 > self.max_prompt_chars:
                break
            lines.append(f"- {fact}")
            used += len(fact) + 3
        lines.reverse()
        return (
            "\nProject memory (facts learned in earlier sessions; verify before relying on them):\n"
            + "\n".join(lines)
        )


# -- learning from a run ---------------------------------------------------------------------------
LEARN_PROMPT = """You maintain a short memory file for a software project. From the run below, list
the durable facts worth remembering in FUTURE sessions on this project: how to run tests/lint/build,
project conventions or structure that were discovered, things the user corrected or insisted on,
and pitfalls that cost time. Rules:
- 0 to 6 facts, one per line, each starting with "- ", each under 160 characters.
- Only facts that will still be true next week. No task narration, no file diffs, no opinions.
- Skip anything already in the existing memory.
- If nothing is worth keeping, reply with exactly: NONE

Existing memory:
{existing}

Run evidence:
{evidence}

Recent conversation (truncated):
{transcript}
"""


def parse_facts(text: str) -> list[str]:
    out: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.upper() == "NONE":
            continue
        m = _LINE.match(line)
        if m:
            out.append(m.group(2)[:300])
    return out[:6]


def deterministic_facts(evidence: dict) -> list[str]:
    """Fallback when no summariser is available: the facts that are certain from the evidence."""
    facts: list[str] = []
    for run in evidence.get("validation_runs", [])[-3:]:
        cmd = str(run.get("command") or "").strip()
        if cmd and run.get("ok"):
            facts.append(f"Validation that passes here: `{cmd}`")
    for text in evidence.get("steering", [])[-3:]:
        text = " ".join(str(text).split())
        if 8 <= len(text) <= 160:
            facts.append(f"User asked during a run: {text}")
    return facts


def noteworthy(evidence: dict) -> bool:
    """Only learn from runs that did something: edits, validation, steering, or failures."""
    return bool(
        evidence.get("changed_files")
        or evidence.get("validation_runs")
        or evidence.get("steering")
        or evidence.get("failures")
    )
