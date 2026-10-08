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
    # Step fields (cheap-model spec §4.1): set by the planner; the loop validates each step.
    files: list[str] = Field(default_factory=list)
    done_when: str = ""
    validation: str | None = None
    attempts: int = 0


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

    # -- task graph (uplift U7) -----------------------------------------------------------
    def unmet(self, task: Task) -> list[str]:
        """Dependencies of ``task`` that are not completed yet."""
        done = {t.id for t in self.tasks if t.status == TaskStatus.COMPLETED}
        return [d for d in task.dependencies if d not in done]

    def ready(self) -> list[Task]:
        """Pending steps whose dependencies are all completed, in plan order."""
        return [t for t in self.tasks if t.status == TaskStatus.PENDING and not self.unmet(t)]

    def graph_issues(self) -> list[str]:
        """Unknown or self dependencies and cycles; empty means the graph is a DAG."""
        ids = {t.id for t in self.tasks}
        issues = []
        for t in self.tasks:
            for d in t.dependencies:
                if d == t.id:
                    issues.append(f"{t.id} depends on itself")
                elif d not in ids:
                    issues.append(f"{t.id} depends on unknown {d}")
        if not issues and sum(len(w) for w in self.waves()) < len(ids):
            issues.append("dependency cycle")
        return issues

    def waves(self) -> list[list[str]]:
        """Topological layers: every step in a wave depends only on earlier waves, so steps in
        one wave are independent. Steps on a cycle never appear."""
        ids = {t.id for t in self.tasks}
        deps = {t.id: {d for d in t.dependencies if d in ids and d != t.id} for t in self.tasks}
        placed: set[str] = set()
        out: list[list[str]] = []
        while True:
            wave = [t.id for t in self.tasks if t.id not in placed and deps[t.id] <= placed]
            if not wave:
                return out
            out.append(wave)
            placed |= set(wave)

    def parallel_safe(self) -> list[list[str]]:
        """Groups of two or more steps that could run at once: same wave, disjoint files."""
        by_id = {t.id: t for t in self.tasks}
        groups = []
        for wave in self.waves():
            group: list[str] = []
            seen: set[str] = set()
            for tid in wave:
                files = set(by_id[tid].files)
                if files and not files & seen:
                    group.append(tid)
                    seen |= files
            if len(group) > 1:
                groups.append(group)
        return groups

    def block_dependents(self, task_id: str) -> list[str]:
        """A step failed: every step that (transitively) needs it is BLOCKED."""
        blocked: list[str] = []
        frontier = {task_id}
        while frontier:
            nxt = set()
            for t in self.tasks:
                if t.status in {TaskStatus.PENDING, TaskStatus.ACTIVE} and frontier & set(
                    t.dependencies
                ):
                    t.status = TaskStatus.BLOCKED
                    blocked.append(t.id)
                    nxt.add(t.id)
            frontier = nxt
        if blocked:
            self.revision += 1
        return blocked

    def render(self) -> str:
        if not self.tasks:
            return "(no plan yet)"
        lines = ["Plan"]
        for t in self.tasks:
            line = f"{_GLYPH[t.status]} {t.id} {t.title}"
            if t.dependencies:
                line += f" (after {', '.join(t.dependencies)})"
            if t.evidence:
                line += f"  — {t.evidence[-1][:80]}"
            lines.append(line)
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> Plan:
        return cls.model_validate(data) if data else cls()
