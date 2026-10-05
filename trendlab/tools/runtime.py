"""ToolRuntime — the only path from a model's tool call to a side effect.

validate args → describe permission → PermissionEngine → (ASK → ApprovalManager)
→ verify the approval binds to this exact operation → execute → emit events.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from trendlab.approvals.manager import ApprovalManager
from trendlab.approvals.models import ApprovalScope, ApprovalStatus
from trendlab.permissions.engine import PermissionEngine, PermissionRequest
from trendlab.permissions.models import Decision, OperationCategory, operation_fingerprint
from trendlab.providers.base import ToolCall
from trendlab.security.scan import find_secrets
from trendlab.telemetry.events import EventBus, EventType
from trendlab.tools.base import PathOutsideProjectError, Tool, ToolContext, ToolResult
from trendlab.tools.registry import ToolRegistry

StateHook = Callable[[str], None]
MUTATING_TOOLS = {"write_file", "patch_file", "apply_patch", "delete_file"}


class ToolRuntime:
    def __init__(
        self,
        registry: ToolRegistry,
        engine: PermissionEngine,
        approvals: ApprovalManager,
        events: EventBus,
        ctx: ToolContext,
        state_hook: StateHook | None = None,
    ) -> None:
        self.registry = registry
        self.engine = engine
        self.approvals = approvals
        self.events = events
        self.ctx = ctx
        self._set_state = state_hook or (lambda _s: None)
        self.changed_files: dict[str, list[str]] = {}  # rel path → diffs this session
        self.validation_runs: list[dict[str, Any]] = []
        self.hooks: Any = None  # trendlab.hooks.HookRunner, attached by the app
        self.diagnostics: Any = None  # trendlab.tools.diagnostics.Diagnostics, attached by the app
        # Called with affected files before a mutation runs (checkpointing).
        self.on_before_mutation: Callable[[list[str]], Awaitable[None]] | None = None

    async def execute(self, call: ToolCall) -> ToolResult:
        tool = self.registry.get(call.name)
        self.events.emit(
            EventType.TOOL_REQUESTED,
            session_id=self.ctx.session_id,
            tool=call.name,
            call_id=call.id,
        )
        if call.name == "_malformed":
            return ToolResult(
                ok=False,
                output="MALFORMED TOOL CALL: "
                + str(call.arguments.get("error"))
                + ". Reply with a valid call; available tools: "
                + ", ".join(self.registry.names()),
            )
        if "_malformed_json" in call.arguments:
            return ToolResult(
                ok=False,
                output=f"MALFORMED TOOL CALL: arguments for {call.name} were not valid JSON. "
                "Resend the call with corrected arguments.",
            )
        if tool is None:
            return ToolResult(
                ok=False,
                output=f"unknown tool: {call.name}; available: " + ", ".join(self.registry.names()),
            )
        try:
            args = tool.parse(call.arguments)
        except ValidationError as exc:
            return ToolResult(ok=False, output=f"invalid arguments for {call.name}: {exc.errors()}")

        perm = tool.permission(args, self.ctx)
        verdict = self.engine.evaluate(perm)
        self.events.emit(
            EventType.PERMISSION_DECIDED,
            session_id=self.ctx.session_id,
            tool=call.name,
            category=perm.category.value,
            risk=perm.risk.value,
            decision=verdict.decision.value,
            reason=verdict.reason,
            mode=self.engine.mode.value,
            fingerprint=perm.fingerprint,
            unsafe_auto=verdict.unsafe_auto,
            command=perm.command,
            files=perm.affected_files,
        )
        if verdict.decision == Decision.DENY:
            return ToolResult(
                ok=False,
                output=f"DENIED: {verdict.reason}",
                data={"denied": True, "category": perm.category.value},
            )
        if verdict.decision == Decision.ASK:
            granted = await self._ask(perm)
            if granted is not None:
                return granted
        if self.hooks is not None:
            blocked = await self.hooks.before_tool(tool.name, perm)
            if blocked:
                return ToolResult(ok=False, output=f"BLOCKED by hook: {blocked}")
        result = await self._run(tool, args, perm)
        if self.hooks is not None:
            await self.hooks.after_tool(tool.name, perm, result)
        return result

    async def _ask(self, perm: PermissionRequest) -> ToolResult | None:
        """Resolve an ASK verdict. Returns a ToolResult to short-circuit, or None to proceed."""
        self.events.emit(
            EventType.PERMISSION_REQUESTED,
            session_id=self.ctx.session_id,
            tool=perm.tool,
            category=perm.category.value,
            risk=perm.risk.value,
            summary=perm.summary,
            fingerprint=perm.fingerprint,
        )
        self._set_state("WAITING_PERMISSION")
        try:
            result = await self.approvals.request(perm, can_persist=self.engine.can_persist(perm))
        finally:
            self._set_state("RUNNING_TOOL")
        if result.status != ApprovalStatus.APPROVED:
            return ToolResult(
                ok=False,
                output=f"NOT EXECUTED: approval {result.status.value}"
                + (f" ({result.reason})" if result.reason else ""),
                data={"approval_id": result.approval_id, "status": result.status.value},
            )
        # The approval must bind to exactly this operation; recompute and compare.
        row = self.approvals.store.get_approval(result.approval_id)
        now_fp = operation_fingerprint(perm.tool, perm.args, perm.cwd)
        if (
            row is None
            or row["fingerprint"] != now_fp
            or row["status"] != ApprovalStatus.APPROVED.value
        ):
            self.events.emit(
                EventType.APPROVAL_REJECTED,
                session_id=self.ctx.session_id,
                approval_id=result.approval_id,
                code="binding_mismatch_at_execution",
            )
            return ToolResult(
                ok=False,
                output="NOT EXECUTED: approval does not match this operation",
                data={"approval_id": result.approval_id, "status": "binding_mismatch"},
            )
        if result.scope == ApprovalScope.SESSION:
            self.engine.add_session_rule(perm, Decision.ALLOW)
        elif result.scope == ApprovalScope.PROJECT:
            self.engine.add_session_rule(perm, Decision.ALLOW)
            self.engine.add_project_rule(perm, Decision.ALLOW)
        return None

    async def _run(self, tool: Tool, args: BaseModel, perm: PermissionRequest) -> ToolResult:
        self._set_state("RUNNING_TOOL")
        started = time.monotonic()
        self.events.emit(
            EventType.TOOL_STARTED,
            session_id=self.ctx.session_id,
            tool=tool.name,
            summary=perm.summary,
        )
        if tool.name in MUTATING_TOOLS and perm.preview and not perm.preview.startswith("("):
            findings = find_secrets(perm.preview)
            if findings:
                self.events.emit(
                    EventType.SECRET_WRITE_BLOCKED,
                    session_id=self.ctx.session_id,
                    tool=tool.name,
                    files=perm.affected_files,
                    findings=findings,
                )
                return ToolResult(
                    ok=False,
                    output="BLOCKED: the content looks like it contains a secret ("
                    + "; ".join(findings[:3])
                    + "). Never write credentials into the repository; reference an environment "
                    "variable or the secrets store instead.",
                )
        if tool.name in MUTATING_TOOLS and self.on_before_mutation is not None:
            from trendlab.agent.plan_gate import PlanRejected

            try:
                await self.on_before_mutation(list(perm.affected_files))
            except PlanRejected as exc:
                return ToolResult(
                    ok=False,
                    output=f"PLAN REJECTED by the user: {exc.reason}. Make no changes; "
                    "stop and explain what you would adjust.",
                    data={"plan_rejected": True, "reason": exc.reason},
                )
            except Exception as exc:  # noqa: BLE001
                if self.engine.unsafe:
                    # In UNSAFE mode the checkpoint is the only safety net, so it is mandatory.
                    return ToolResult(
                        ok=False, output=f"BLOCKED: checkpoint failed in UNSAFE mode ({exc})"
                    )
        try:
            result = await tool.run(args, self.ctx)
        except PathOutsideProjectError as exc:
            result = ToolResult(ok=False, output=f"DENIED: {exc}")
        except Exception as exc:  # noqa: BLE001 — surface as observation, not crash
            result = ToolResult(ok=False, output=f"tool error: {exc.__class__.__name__}: {exc}")
        self.events.emit(
            EventType.TOOL_COMPLETED,
            session_id=self.ctx.session_id,
            tool=tool.name,
            ok=result.ok,
            duration_ms=int((time.monotonic() - started) * 1000),
            **{k: v for k, v in result.data.items() if k in {"exit_code", "sha256"}},
        )
        if result.ok and tool.name in MUTATING_TOOLS:
            self.events.emit(
                EventType.FILE_CHANGED,
                session_id=self.ctx.session_id,
                files=perm.affected_files,
                tool=tool.name,
                diff=result.data.get("diff"),
            )
            for f in perm.affected_files:
                self.changed_files.setdefault(f, []).append(result.data.get("diff") or "")
            if self.diagnostics is not None:
                try:
                    wrapper = None
                    if self.ctx.sandbox is not None and self.ctx.sandbox.active:
                        wrapper = lambda c: self.ctx.sandbox.wrap(c, cwd=self.ctx.project_root)  # noqa: E731
                    report = await self.diagnostics.run(
                        list(perm.affected_files), argv_wrapper=wrapper
                    )
                except Exception:  # noqa: BLE001 — diagnostics never break an edit
                    report = ""
                if report:
                    result.output = f"{result.output}\n\n{report}"
                    result.data["diagnostics"] = report
                    self.events.emit(
                        EventType.DIAGNOSTICS,
                        session_id=self.ctx.session_id,
                        files=perm.affected_files,
                        report=report[:1000],
                    )
        ran_validation = "exit_code" in result.data and (
            tool.name == "run_tests"
            or (tool.name == "shell" and perm.category == OperationCategory.RUN_TESTS)
        )
        if ran_validation:
            self.validation_runs.append(
                {
                    "command": perm.command,
                    "ok": result.ok,
                    "exit_code": result.data.get("exit_code"),
                    "tail": result.output[-600:],
                }
            )
        return result

    def describe(self) -> dict[str, Any]:
        return {"tools": self.registry.names(), "mode": self.engine.mode.value}
