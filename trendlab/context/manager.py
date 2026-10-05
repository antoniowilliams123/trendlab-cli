"""Context engine (spec §18, §20, §21): assemble a budgeted prompt and compact when needed.

Layers (priority order): system policy → project instructions → repository map → compaction
summary (structured state) → plan → recent conversation. Each layer has a token budget; the
conversation tail is trimmed oldest-first, never mid-tool-exchange.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from trendlab.config.schema import ContextConfig
from trendlab.context.compaction import CompactionRecord, compaction_prompt, deterministic_summary
from trendlab.telemetry.events import EventBus, EventType

Summarizer = Callable[[list[dict[str, Any]]], Awaitable[str]]


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def message_tokens(m: dict[str, Any]) -> int:
    total = estimate_tokens(str(m.get("content") or ""))
    for tc in m.get("tool_calls") or []:
        total += estimate_tokens(tc["function"]["arguments"]) + 10
    return total + 4


class ContextManager:
    def __init__(
        self,
        config: ContextConfig,
        events: EventBus,
        session_id: str,
        *,
        system_prompt: str,
        context_window: int | None = None,
        summarizer: Summarizer | None = None,
    ) -> None:
        self.config = config
        self.events = events
        self.session_id = session_id
        self.system_prompt = system_prompt
        self.context_window = context_window or config.default_context_window
        self.summarizer = summarizer
        self.repo_map_text: str = ""
        self.plan_text: str = ""
        self.summary: CompactionRecord | None = None
        self.messages: list[dict[str, Any]] = []  # conversation after the system prompt
        self.structured: dict[str, Any] = {}
        self.compactions = 0

    # -- assembly --------------------------------------------------------------------------------
    def build(self) -> list[dict[str, Any]]:
        parts = [self.system_prompt]
        if self.repo_map_text:
            budget = self.config.repo_map_budget_tokens * 4
            parts.append("Repository map:\n" + self.repo_map_text[:budget])
        if self.summary is not None:
            parts.append(
                "Summary of earlier work in this session (authoritative state):\n"
                + self.summary.summary
            )
        if self.plan_text:
            parts.append("Current plan:\n" + self.plan_text)
        system = {"role": "system", "content": "\n\n".join(parts)}
        return [system, *self._recent()]

    def _recent(self) -> list[dict[str, Any]]:
        budget = min(
            self.config.recent_messages_budget_tokens,
            int(self.context_window * self.config.compact_threshold),
        )
        kept: list[dict[str, Any]] = []
        total = 0
        for m in reversed(self.messages):
            t = message_tokens(m)
            if kept and total + t > budget:
                break
            kept.append(m)
            total += t
        kept.reverse()
        return _align_to_boundary(kept, self.messages)

    def estimate(self) -> int:
        return sum(message_tokens(m) for m in self.build())

    def estimate_full(self) -> int:
        """Tokens if the whole retained history were sent (what compaction protects against)."""
        system = self.build()[0]
        return message_tokens(system) + sum(message_tokens(m) for m in self.messages)

    def history_tokens(self) -> int:
        return sum(message_tokens(m) for m in self.messages)

    def needs_compaction(self) -> bool:
        """Compact when the full prompt would cross the threshold *and* the conversation itself
        is big enough that compacting it helps (avoids churning on a tiny tail)."""
        if not self.config.auto_compact:
            return False
        threshold = self.context_window * self.config.compact_threshold
        min_history = max(self.config.min_compaction_tokens, int(self.context_window * 0.1))
        return self.estimate_full() >= threshold and self.history_tokens() >= min_history

    # -- compaction -------------------------------------------------------------------------------
    async def compact(self, *, keep_last: int = 6, force: bool = False) -> CompactionRecord | None:
        if len(self.messages) <= keep_last and not force:
            return None
        cut = max(0, len(self.messages) - keep_last)
        cut = _boundary_index(self.messages, cut)
        old, recent = self.messages[:cut], self.messages[cut:]
        if not old:
            return None
        tokens_before = sum(message_tokens(m) for m in self.messages)
        structured = dict(self.structured)
        if self.summary is not None:
            structured["previous_summary"] = self.summary.summary[:4000]
        source = "deterministic"
        summary = None
        if self.summarizer is not None:
            try:
                summary = await self.summarizer(compaction_prompt(old, structured))
                source = "model"
            except Exception:  # noqa: BLE001 — never lose state because the summarizer failed
                summary = None
        if not summary:
            summary = deterministic_summary(old, structured)
        self.summary = CompactionRecord(
            summary=summary,
            structured=structured,
            messages_compacted=len(old),
            tokens_before=tokens_before,
            source=source,
        )
        self.messages = recent
        self.summary.tokens_after = sum(message_tokens(m) for m in self.build())
        self.compactions += 1
        self.events.emit(
            EventType.CONTEXT_COMPACTED,
            session_id=self.session_id,
            source=source,
            messages_compacted=len(old),
            tokens_before=tokens_before,
            tokens_after=self.summary.tokens_after,
        )
        return self.summary

    def status(self) -> dict[str, Any]:
        return {
            "estimated_tokens": self.estimate(),
            "history_tokens": self.estimate_full(),
            "context_window": self.context_window,
            "messages": len(self.messages),
            "compactions": self.compactions,
            "has_summary": self.summary is not None,
            "repo_map_chars": len(self.repo_map_text),
        }


def _boundary_index(messages: list[dict[str, Any]], idx: int) -> int:
    """Move ``idx`` forward so it does not split an assistant tool call from its tool results."""
    while idx < len(messages) and messages[idx].get("role") == "tool":
        idx += 1
    return idx


def _align_to_boundary(
    kept: list[dict[str, Any]], all_msgs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not kept:
        return kept
    start = len(all_msgs) - len(kept)
    start = _boundary_index(all_msgs, start)
    return all_msgs[start:]
