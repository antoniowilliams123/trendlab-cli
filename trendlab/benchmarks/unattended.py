"""Unattended runs (benchmarks, canary, replay, meta-loop drafts): nobody can answer, so every
approval is denied and every question gets a fixed answer at once. Both count as interventions.
Without this an unattended run that asks blocks until the request times out."""

from __future__ import annotations

from typing import Any

from trendlab.telemetry.events import EventType

UNATTENDED_ANSWER = (
    "No human is available to answer (unattended run). Decide from the task as written; when an "
    "action is irreversible or outside what the task clearly asks, do not take it."
)


class Unattended:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.approvals_denied = 0
        self.questions_answered = 0
        self._unsub = app.events.subscribe(self)

    @property
    def interventions(self) -> int:
        return self.approvals_denied + self.questions_answered

    def __call__(self, event) -> None:
        if event.type == EventType.APPROVAL_REQUESTED:
            self.approvals_denied += 1
            try:
                self.app.approvals.decide(
                    event.data["approval_id"], "deny", via="unattended", trusted=True
                )
            except Exception:  # noqa: BLE001
                pass
        elif event.type == EventType.QUESTION_ASKED:
            self.questions_answered += 1
            try:
                self.app.approvals.answer(
                    event.data["approval_id"], UNATTENDED_ANSWER, via="unattended", trusted=True
                )
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        self._unsub()
