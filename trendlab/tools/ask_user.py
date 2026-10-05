"""``ask_user``: the model asks the human a question and waits for the answer.

Questions ride the approval infrastructure: they are presented through every
active channel (terminal and phone), expire like approvals, and are audited.
A question is a request for *text*, never for permission — it cannot approve
anything.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from trendlab.approvals.manager import ApprovalManager
from trendlab.approvals.models import ApprovalStatus
from trendlab.permissions.engine import PermissionRequest
from trendlab.permissions.models import OperationCategory
from trendlab.tools.base import Tool, ToolContext, ToolResult


class AskUserInput(BaseModel):
    question: str = Field(
        description="One clear question. Offer options when there are a few sensible answers."
    )
    options: list[str] = Field(default_factory=list, description="Optional short answer choices")


class AskUserTool(Tool):
    name = "ask_user"
    description = (
        "Ask the user a clarifying question and wait for their answer (they may reply from their "
        "phone). Use sparingly: only when the task cannot proceed without a decision from the user."
    )
    input_model = AskUserInput

    def __init__(self, approvals: ApprovalManager) -> None:
        self.approvals = approvals

    def permission(self, args: AskUserInput, ctx: ToolContext) -> PermissionRequest:
        return PermissionRequest(
            tool=self.name,
            category=OperationCategory.READ_ONLY,
            summary=f"Question: {args.question[:80]}",
            cwd=str(ctx.project_root),
            args=args.model_dump(),
            task_id=ctx.task_id,
        )

    async def run(self, args: AskUserInput, ctx: ToolContext) -> ToolResult:
        result = await self.approvals.ask(args.question, args.options, task_id=ctx.task_id)
        if result.status == ApprovalStatus.APPROVED:
            return ToolResult(
                ok=True, output=f"USER ANSWER: {result.reason}", data={"answer": result.reason}
            )
        return ToolResult(
            ok=False,
            output=f"NO ANSWER: question {result.status.value}; proceed with your "
            f"best judgement or stop and explain.",
            data={"status": result.status.value},
        )
