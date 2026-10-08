"""ToolRuntime — the only path from a model's tool call to a side effect.

validate args → describe permission → PermissionEngine → (ASK → ApprovalManager)
→ verify the approval binds to this exact operation → execute → emit events.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from pathlib import Path
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
from trendlab.tools.health import RESULT_TOOLS, postcondition
from trendlab.tools.registry import ToolRegistry

StateHook = Callable[[str], None]
MUTATING_TOOLS = {"write_file", "patch_file", "apply_patch", "delete_file"}
# Tools whose output is content from outside the user's request (scanned for injection).
SCANNED_TOOLS = {
    "read_file",
    "shell",
    "run_tests",
    "web_fetch",
    "web_search",
    "search_text",
    "git_log",
    "git_diff",
}
GIT_TOOLS = {"git_status", "git_diff", "git_log"}


def _in_git_repo(root: Path) -> bool:
    p = root.resolve()
    for candidate in (p, *p.parents):
        if (candidate / ".git").exists():
            return True
    return False


TAINT_GATED = {
    OperationCategory.FILE_DELETE,
    OperationCategory.NETWORK,
    OperationCategory.PACKAGE_INSTALL,
    OperationCategory.DESTRUCTIVE,
    OperationCategory.OUTSIDE_PROJECT,
}


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
        # Tiered output (cheap-model spec §2): budgets + optional screener, attached by the app.
        self.tool_budgets: dict[str, int] = {}
        self.screener: Any = None  # async (text, meta) -> ToolOutput | None
        self.screener_threshold_tokens: int = 3000
        self.baselines: Any = None  # trendlab.tools.views.tiering.Baselines
        from trendlab.tools.health import ToolHealth

        self.health = ToolHealth()  # per-tool counters + circuit breaker (uplift U4)
        # Prompt-injection taint: set when a tool result addressed the agent; until reset (run
        # start), harmful categories need approval even in AUTO mode.
        self.tainted: list[dict[str, str]] = []
        self.secrets_touched: list[str] = []  # secret-bearing paths read this run (U11)
        # Called with affected files before a mutation runs (checkpointing).
        self.on_before_mutation: Callable[[list[str]], Awaitable[None]] | None = None

    async def execute(self, call: ToolCall) -> ToolResult:
        with self.events.span(f"tool:{call.name}", self.ctx.session_id, tool=call.name):
            return await self._execute(call)

    # -- tool routing (context-aware tool selection policy) ---------------------------------
    def unavailable(self, name: str) -> str | None:
        """Why ``name`` cannot work in this project, or None when it can. Tools that cannot
        work are not offered to the model at all, so it never has to discover that by failing."""
        root = self.ctx.project_root
        if name in GIT_TOOLS and not _in_git_repo(root):
            return "this project is not a git repository; use list_directory/read_file instead"
        if name == "run_tests" and not (self.ctx.validation_commands or {}):
            return "no test command is known for this project; run the tests with shell"
        return None

    def _unresolved_refs(self, files: list[str], result: ToolResult) -> list[str]:
        """U17: imports and project names added by this edit that resolve nowhere."""
        from trendlab.agent.symbols import added_lines, unresolved

        root = self.ctx.project_root
        out: list[str] = []
        for f in files:
            if not f.endswith(".py"):
                continue
            rel = f
            try:
                rel = str((root / f).resolve().relative_to(root.resolve()))
            except ValueError:
                continue
            diff = result.data.get("diff") or ""
            lines = added_lines(diff) if diff else None
            try:
                out += unresolved(root, rel, only_lines=lines)
            except Exception:  # noqa: BLE001 — the check must never break an edit
                continue
        return out

    def _fallback_call(self, call: ToolCall) -> ToolCall | None:
        """A call that does the same job with another tool, when one exists."""
        if call.name == "run_tests":
            kind = str(call.arguments.get("kind") or "test")
            cmd = (self.ctx.validation_commands or {}).get(kind)
            if cmd:
                extra = str(call.arguments.get("extra_args") or "").strip()
                return ToolCall(
                    id=f"{call.id}-fallback",
                    name="shell",
                    arguments={"command": f"{cmd} {extra}".strip()},
                )
        if call.name == "list_directory":
            path = str(call.arguments.get("path") or ".").rstrip("/")
            return ToolCall(
                id=f"{call.id}-fallback", name="glob", arguments={"pattern": f"{path}/*"}
            )
        return None

    def visible_schemas(self) -> list[dict[str, Any]]:
        return [
            t.schema()
            for name, t in self.registry._tools.items()
            if not self.unavailable(name)  # noqa: SLF001
        ]

    async def _execute(self, call: ToolCall) -> ToolResult:
        tool = self.registry.get(call.name)
        why = self.unavailable(call.name) if tool is not None else None
        if why:
            return self._skip(call, "unavailable_here", f"UNAVAILABLE: {call.name} — {why}")
        self.events.emit(
            EventType.TOOL_REQUESTED,
            session_id=self.ctx.session_id,
            tool=call.name,
            call_id=call.id,
        )
        if call.name == "_malformed":
            return self._skip(
                call,
                "malformed",
                "MALFORMED TOOL CALL: "
                + str(call.arguments.get("error"))
                + ". Reply with a valid call; available tools: "
                + ", ".join(self.registry.names()),
            )
        if "_malformed_json" in call.arguments:
            return self._skip(
                call,
                "malformed",
                f"MALFORMED TOOL CALL: arguments for {call.name} were not valid JSON. "
                "Resend the call with corrected arguments.",
            )
        if tool is None:
            return self._skip(
                call,
                "unknown_tool",
                f"unknown tool: {call.name}; available: " + ", ".join(self.registry.names()),
            )
        paused = self.health.blocked(tool.name)
        if paused:
            self.health.record(
                tool.name, ok=False, failed=False, skipped=True, ms=0, error="circuit open"
            )
            fallback = self._fallback_call(call)
            if fallback is not None and not self.health.blocked(fallback.name):
                # automatic tool fallback (U4): the same job through a working tool
                self.events.emit(
                    EventType.RECOVERY,
                    session_id=self.ctx.session_id,
                    failure="TOOL_CIRCUIT_OPEN",
                    action="tool_fallback",
                    tool=call.name,
                    fallback=fallback.name,
                )
                result = await self._execute(fallback)
                result.output = (
                    f"[{call.name} is paused; ran it through {fallback.name} instead]\n"
                    + (result.output or "")
                )
                return result
            return self._skip(call, "circuit_open", f"PAUSED: {paused}", detail=tool.name)
        try:
            args = tool.parse(call.arguments)
        except ValidationError as exc:
            missing = [".".join(str(x) for x in e.get("loc", ())) for e in exc.errors()][:4]
            given = ", ".join(sorted(call.arguments)) or "nothing"
            self.events.emit(
                EventType.TOOL_SKIPPED,
                session_id=self.ctx.session_id,
                tool=call.name,
                reason="invalid_args",
                detail="needs " + ", ".join(missing),
                message=f"given: {given}",
            )
            return ToolResult(ok=False, output=f"invalid arguments for {call.name}: {exc.errors()}")

        perm = tool.permission(args, self.ctx)
        verdict = self.engine.evaluate(perm)
        if self.tainted and verdict.decision == Decision.ALLOW and perm.category in TAINT_GATED:
            verdict = type(verdict)(
                Decision.ASK,
                "approval required: this run read content that tried to instruct the agent",
                verdict.risk,
            )
        if verdict.decision == Decision.ALLOW and perm.category == OperationCategory.NETWORK:
            from trendlab.security.exfil import touches_secret

            named = touches_secret(perm.affected_files, perm.command)
            if named or self.secrets_touched:
                verdict = type(verdict)(
                    Decision.ASK,
                    "approval required: this network call could carry secrets out ("
                    + ", ".join((named or self.secrets_touched)[:3])
                    + ")",
                    verdict.risk,
                )
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
            return self._skip(
                call,
                "denied",
                f"DENIED: {verdict.reason}",
                detail=_detail(tool.name, call.arguments, perm),
                data={"denied": True, "category": perm.category.value},
            )
        if verdict.decision == Decision.ASK:
            granted = await self._ask(perm)
            if granted is not None:
                self.events.emit(
                    EventType.TOOL_SKIPPED,
                    session_id=self.ctx.session_id,
                    tool=call.name,
                    reason="not_approved",
                    detail=_detail(tool.name, call.arguments, perm),
                    message=granted.output[:300],
                )
                return granted
        if self.hooks is not None:
            blocked = await self.hooks.before_tool(tool.name, perm)
            if blocked:
                return self._skip(
                    call,
                    "hook_blocked",
                    f"BLOCKED by hook: {blocked}",
                    detail=_detail(tool.name, call.arguments, perm),
                )
        result = await self._run(tool, args, perm, call_id=call.id)
        if self.hooks is not None:
            await self.hooks.after_tool(tool.name, perm, result)
        return result

    def _skip(
        self,
        call: ToolCall,
        reason: str,
        message: str,
        *,
        detail: str = "",
        data: dict[str, Any] | None = None,
    ) -> ToolResult:
        """A call that never ran: tell the model why, and show the person (tool.skipped)."""
        self.events.emit(
            EventType.TOOL_SKIPPED,
            session_id=self.ctx.session_id,
            tool=call.name,
            reason=reason,
            detail=detail,
            message=message[:300],
        )
        return ToolResult(ok=False, output=message, data=data or {})

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

    async def _run(
        self, tool: Tool, args: BaseModel, perm: PermissionRequest, call_id: str = ""
    ) -> ToolResult:
        self._set_state("RUNNING_TOOL")
        started = time.monotonic()
        self.events.emit(
            EventType.TOOL_STARTED,
            session_id=self.ctx.session_id,
            tool=tool.name,
            summary=perm.summary,
            detail=_detail(tool.name, args.model_dump(), perm),
            command=perm.command,
            files=list(perm.affected_files),
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
            from trendlab.agent.scope import ChangeScopeRefused

            try:
                await self.on_before_mutation(list(perm.affected_files))
            except ChangeScopeRefused as exc:
                return self._skip(
                    ToolCall(id="", name=tool.name, arguments={}),
                    "outside_change_scope",
                    f"REFUSED: {', '.join(exc.files)} is outside the change scope the user set "
                    f"({', '.join(exc.allow)}). Do not edit it; work within the allowed paths, or "
                    "say in your answer why the task needs it.",
                    detail=", ".join(exc.files),
                )
            except PlanRejected as exc:
                return self._skip(
                    ToolCall(id="", name=tool.name, arguments={}),
                    "plan_rejected",
                    f"PLAN REJECTED by the user: {exc.reason}. Make no changes; "
                    "stop and explain what you would adjust.",
                    detail=", ".join(perm.affected_files),
                    data={"plan_rejected": True, "reason": exc.reason},
                )
            except Exception as exc:  # noqa: BLE001
                if self.engine.unsafe:
                    # In UNSAFE mode the checkpoint is the only safety net, so it is mandatory.
                    return self._skip(
                        ToolCall(id="", name=tool.name, arguments={}),
                        "checkpoint_failed",
                        f"BLOCKED: checkpoint failed in AUTO mode ({exc})",
                        detail=", ".join(perm.affected_files),
                    )
        call_started = time.monotonic()

        def progress(tail: str) -> None:
            self.events.emit(
                EventType.TOOL_OUTPUT,
                session_id=self.ctx.session_id,
                tool=tool.name,
                tail=tail[-1200:],
                elapsed_s=round(time.monotonic() - call_started, 1),
            )

        self.ctx.progress = progress
        try:
            result = await tool.run(args, self.ctx)
        except PathOutsideProjectError as exc:
            result = ToolResult(ok=False, output=f"DENIED: {exc}")
        except Exception as exc:  # noqa: BLE001 — surface as observation, not crash
            result = ToolResult(ok=False, output=f"tool error: {exc.__class__.__name__}: {exc}")
        finally:
            self.ctx.progress = None
        duration_ms = int((time.monotonic() - call_started) * 1000)
        if result.ok and perm.category != OperationCategory.NETWORK:
            from trendlab.security.exfil import touches_secret

            touched = touches_secret(perm.affected_files, perm.command)
            if touched:
                self.secrets_touched = sorted(set(self.secrets_touched) | set(touched))
        if tool.name in SCANNED_TOOLS and result.output:
            from trendlab.security.injection import scan, warning

            hits = scan(result.output)
            if hits:
                self.tainted.extend(hits)
                result.output = warning(hits) + result.output
                result.data["injection_suspected"] = hits
                self.events.emit(
                    EventType.INJECTION_SUSPECTED,
                    session_id=self.ctx.session_id,
                    tool=tool.name,
                    hits=hits,
                )
        error_text = (result.output or "")[:200] if not result.ok else ""
        tool_error = (
            (not result.ok)
            and (tool.name not in RESULT_TOOLS or result.data.get("timed_out") is True)
            and (
                error_text.startswith(
                    ("tool error", "timed out", "network error", "command timed out")
                )
                or result.data.get("timed_out") is True
                or tool.name not in RESULT_TOOLS
                and not error_text.startswith(
                    ("not a file", "DENIED", "BLOCKED", "invalid", "refused", "no ")
                )
            )
        )
        opened = self.health.record(
            tool.name,
            ok=result.ok,
            failed=bool(tool_error),
            skipped=False,
            ms=duration_ms,
            error=error_text,
            timed_out=bool(result.data.get("timed_out")),
        )
        if opened:
            self.events.emit(
                EventType.TOOL_CIRCUIT_OPENED,
                session_id=self.ctx.session_id,
                tool=tool.name,
                failures=self.health.stats[tool.name].consecutive_failures,
                cooldown_s=self.health.cooldown_s,
                last_error=error_text,
            )
        if result.ok and tool.name in MUTATING_TOOLS and not result.data.get("unchanged"):
            problems = []
            for f in perm.affected_files:
                problem = postcondition(self.ctx.project_root / f)
                if problem:
                    problems.append(problem)
            refs = self._unresolved_refs(perm.affected_files, result) if not problems else []
            if refs:
                result.output = (
                    f"{result.output}\n\nUNRESOLVED REFERENCE — this edit uses names that do not "
                    "exist: " + "; ".join(refs[:6]) + ". Use the real name (read the module) or "
                    "create what is missing."
                )
                result.data["unresolved"] = refs
                self.events.emit(
                    EventType.TOOL_POSTCONDITION_FAILED,
                    session_id=self.ctx.session_id,
                    tool=tool.name,
                    files=perm.affected_files,
                    problems=refs,
                    kind="unresolved_reference",
                )
            if problems:
                result.output = (
                    f"{result.output}\n\nPOSTCONDITION FAILED — the file no longer parses: "
                    + "; ".join(problems)
                    + ". Fix it before doing anything else."
                )
                result.data["postcondition_failed"] = problems
                self.events.emit(
                    EventType.TOOL_POSTCONDITION_FAILED,
                    session_id=self.ctx.session_id,
                    tool=tool.name,
                    files=perm.affected_files,
                    problems=problems,
                )
        tiered = await self._tier(tool.name, args, result, call_id or f"{tool.name}-{int(started)}")
        out_lines = [ln for ln in (result.output or "").splitlines() if ln.strip()]
        self.events.emit(
            EventType.TOOL_COMPLETED,
            session_id=self.ctx.session_id,
            tool=tool.name,
            ok=result.ok,
            duration_ms=int((time.monotonic() - started) * 1000),
            lines=len(out_lines),
            preview=(" ⏎ ".join(out_lines[:2]))[:200]
            if result.ok and tool.name != "read_file"
            else "",
            error=(out_lines[0] if out_lines else "")[:300] if not result.ok else "",
            **{k: v for k, v in result.data.items() if k in {"exit_code", "sha256"}},
        )
        if tiered is not None:
            self.events.emit(
                EventType.TOOL_OUTPUT_TIERED,
                session_id=self.ctx.session_id,
                tool=tool.name,
                call_id=call_id,
                parser=tiered.parser,
                kind=tiered.kind,
                raw_chars=result.data.get("raw_chars", 0),
                shown_chars=len(result.output or ""),
                tier3_ref=tiered.tier3_ref,
                anomaly=tiered.stats.get("anomaly"),
            )
        if result.ok and tool.name in MUTATING_TOOLS and not result.data.get("unchanged"):
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
                    result.output = f"{result.output}\n\n{self._lint_view(report)}"
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

    async def _tier(self, tool: str, args: BaseModel, result: ToolResult, call_id: str):
        if not self.tool_budgets:
            return None
        from trendlab.tools.views.tiering import tier_result

        try:
            return await tier_result(
                tool=tool,
                args=args.model_dump(),
                result=result,
                call_id=call_id,
                project_root=self.ctx.project_root,
                budgets=self.tool_budgets,
                screener=self.screener,
                screener_threshold_tokens=self.screener_threshold_tokens,
                baselines=self.baselines,
            )
        except Exception:  # noqa: BLE001 — tiering never breaks a tool result
            return None

    def _lint_view(self, report: str) -> str:
        """Diagnostics after an edit, shown as a lint view (counts by code, grouped by file)."""
        limit = (self.tool_budgets.get("diagnostics") or 400) * 4
        if len(report) <= limit:
            return report
        from trendlab.tools.views import tier_output

        return tier_output(report, {"hint": "lint"}).render(limit)

    def describe(self) -> dict[str, Any]:
        return {"tools": self.registry.names(), "mode": self.engine.mode.value}


def _detail(tool: str, args: dict[str, Any], perm: PermissionRequest | None = None) -> str:
    """Short human detail for an activity line: the command, path, pattern, query or question."""
    if perm is not None and perm.command:
        return f"$ {perm.command}"
    for key in ("command", "path", "pattern", "query", "url", "question", "action", "name"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            if key == "command":
                return f"$ {val}"
            if tool == "background_process" and key == "action":
                return f"{val} {args.get('command') or args.get('id') or ''}".strip()
            if tool == "task":
                titles = args.get("titles") or []
                return f"{val} {', '.join(titles[:3])}".strip() if titles else val
            return val
    if perm is not None and perm.affected_files:
        return ", ".join(perm.affected_files[:3])
    return ""
