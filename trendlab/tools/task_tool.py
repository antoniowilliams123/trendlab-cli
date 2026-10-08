"""``task`` tool: the model's handle on the structured plan (spec §13, §14)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from trendlab.agent.tasks import Plan, TaskStatus
from trendlab.permissions.engine import PermissionRequest
from trendlab.permissions.models import OperationCategory
from trendlab.telemetry.events import EventBus, EventType
from trendlab.tools.base import Tool, ToolContext, ToolResult


class TaskInput(BaseModel):
    action: str = Field(description="plan | add | update | complete | fail | block | remove | list")
    titles: list[str] = Field(
        default_factory=list, description="For 'plan': the full ordered task list"
    )
    title: str = ""
    description: str = ""
    task_id: str = ""
    evidence: str = Field(
        default="", description="Evidence for complete/fail, e.g. 'pytest: 20 passed'"
    )


class TaskTool(Tool):
    name = "task"
    description = (
        "Create and maintain the task plan. Use action='plan' with titles for a new plan before "
        "non-trivial work; 'update' with task_id to mark it active; 'complete' with evidence "
        "when done."
    )
    input_model = TaskInput

    def __init__(self, plan: Plan, events: EventBus) -> None:
        self.plan = plan
        self.events = events
        self._replans = 0  # 'plan' calls while tasks were already open (weak-model loop guard)

    def permission(self, args: TaskInput, ctx: ToolContext) -> PermissionRequest:
        return PermissionRequest(
            tool=self.name,
            category=OperationCategory.READ_ONLY,
            summary=f"Plan: {args.action}",
            cwd=str(ctx.project_root),
            args=args.model_dump(),
            task_id=ctx.task_id,
        )

    async def run(self, args: TaskInput, ctx: ToolContext) -> ToolResult:
        plan, a = self.plan, args.action.lower()
        if a == "plan":
            if not args.titles:
                return ToolResult(ok=False, output="'plan' needs titles")
            if plan.open:
                self._replans += 1
                if self._replans >= 2:
                    self.events.emit(
                        EventType.GUARD_FIRED,
                        session_id=ctx.session_id,
                        guard="replan_blocked",
                        model=None,
                        role=ctx.agent_role or "main",
                    )
                    return ToolResult(
                        ok=False,
                        output="A plan already exists and its tasks are open — do NOT re-plan. "
                        "Work on the active task with the other tools, then action='complete' "
                        "with evidence, or action='fail'/'block' with a reason. Current plan:\n"
                        + plan.render(),
                    )
            else:
                self._replans = 0
            plan.replace(args.titles)
            if plan.open:
                plan.update(plan.open[0].id, status=TaskStatus.ACTIVE)
        elif a == "add":
            if not args.title:
                return ToolResult(ok=False, output="'add' needs a title")
            plan.add(args.title, args.description)
        elif a in {"update", "complete", "fail", "block", "remove"}:
            if plan.get(args.task_id) is None:
                return ToolResult(
                    ok=False, output=f"unknown task_id {args.task_id!r}; use action='list'"
                )
            if a == "remove":
                plan.remove(args.task_id)
            else:
                status = {
                    "update": TaskStatus.ACTIVE,
                    "complete": TaskStatus.COMPLETED,
                    "fail": TaskStatus.FAILED,
                    "block": TaskStatus.BLOCKED,
                }[a]
                if a == "complete" and not args.evidence:
                    return ToolResult(
                        ok=False, output="'complete' requires evidence (what proves it is done?)"
                    )
                plan.update(
                    args.task_id,
                    status=status,
                    title=args.title or None,
                    description=args.description or None,
                    evidence=args.evidence or None,
                )
                if a == "complete":
                    nxt = next((t for t in plan.open if t.status == TaskStatus.PENDING), None)
                    if nxt and plan.active is None:
                        plan.update(nxt.id, status=TaskStatus.ACTIVE)
        elif a != "list":
            return ToolResult(ok=False, output=f"unknown action {a!r}")
        self.events.emit(
            EventType.PLAN_UPDATED,
            session_id=ctx.session_id,
            revision=plan.revision,
            open=len(plan.open),
            total=len(plan.tasks),
            plan=plan.to_json(),
        )
        return ToolResult(ok=True, output=plan.render(), data={"plan": plan.to_json()})
