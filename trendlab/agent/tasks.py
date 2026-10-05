"""Structured task plan (spec §13, §49). The plan is state, not prose; the model edits it via the
``task`` tool and the UI renders it."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


_GLYPH = {
    TaskStatus.PENDING: "[ ]",
    TaskStatus.ACTIVE: "[→]",
    TaskStatus.BLOCKED: "[!]",
    TaskStatus.COMPLETED: "[✓]",
    TaskStatus.FAILED: "[✗]",
    TaskStatus.CANCELED: "[-]",
}


class Task(BaseModel):
    id: str
    title: str
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    dependencies: list[str] = Field(default_factory=list)
    assigned_agent: str | None = None
    evidence: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    tasks: list[Task] = Field(default_factory=list)
    revision: int = 0
    _next: int = 1

    def model_post_init(self, __context: Any) -> None:
        nums = [
            int(t.id.split("-")[1])
            for t in self.tasks
            if t.id.startswith("T-") and t.id[2:].isdigit()
        ]
        self._next = (max(nums) + 1) if nums else 1

    # -- mutation -------------------------------------------------------------------
    def add(self, title: str, description: str = "", dependencies: list[str] | None = None) -> Task:
        task = Task(
            id=f"T-{self._next}",
            title=title.strip(),
            description=description.strip(),
            dependencies=dependencies or [],
        )
        self._next += 1
        self.tasks.append(task)
        self.revision += 1
        return task

    def get(self, task_id: str) -> Task | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    def update(
        self,
        task_id: str,
        *,
        status: TaskStatus | None = None,
        title: str | None = None,
        description: str | None = None,
        evidence: str | None = None,
    ) -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None
        if status is not None:
            if status == TaskStatus.ACTIVE:
                for other in self.tasks:
                    if other.status == TaskStatus.ACTIVE:
                        other.status = TaskStatus.PENDING
            task.status = status
        if title:
            task.title = title.strip()
        if description is not None:
            task.description = description.strip()
        if evidence:
            task.evidence.append(evidence.strip()[:500])
        self.revision += 1
        return task

    def remove(self, task_id: str) -> bool:
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if t.id != task_id]
        self.revision += 1
        return len(self.tasks) < before

    def replace(self, titles: list[str]) -> None:
        """Replace all non-completed tasks with a fresh list (plan revision)."""
        keep = [t for t in self.tasks if t.status == TaskStatus.COMPLETED]
        self.tasks = keep
        for title in titles:
            self.add(title)

    # -- queries ---------------------------------------------------------------------
    @property
    def active(self) -> Task | None:
        return next((t for t in self.tasks if t.status == TaskStatus.ACTIVE), None)

    @property
    def open(self) -> list[Task]:
        return [
            t
            for t in self.tasks
            if t.status in {TaskStatus.PENDING, TaskStatus.ACTIVE, TaskStatus.BLOCKED}
        ]

    @property
    def done(self) -> bool:
        return bool(self.tasks) and not self.open

    def render(self) -> str:
        if not self.tasks:
            return "(no plan yet)"
        lines = ["Plan"]
        for t in self.tasks:
            line = f"{_GLYPH[t.status]} {t.id} {t.title}"
            if t.evidence:
                line += f"  — {t.evidence[-1][:80]}"
            lines.append(line)
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> Plan:
        return cls.model_validate(data) if data else cls()
