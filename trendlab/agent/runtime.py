"""The agent loop (spec §12): context → model → tools → observe → evaluate → repeat.

Permission waits happen *inside* a tool call, so a remote approval pauses only
that operation while session and conversation state stay intact. Completion is
decided by the evaluator from evidence, not by the model saying "done".
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from trendlab.agent.evaluator import CompletionEvaluator, EvidenceSummary, final_report
from trendlab.agent.loop_detector import LoopDetector
from trendlab.agent.recovery import (
    NO_PROGRESS_FEEDBACK,
    FailureClass,
    RecoveryAction,
    classify_provider_error,
    recovery_for,
)
from trendlab.agent.rescue import prose_outside_calls, rescue_text_tool_calls
from trendlab.agent.state import AgentState, AgentStateMachine
from trendlab.agent.tasks import Plan, Task, TaskStatus
from trendlab.config.schema import LimitsConfig
from trendlab.context.manager import ContextManager
from trendlab.providers.base import (
    ModelResponse,
    ProviderContextOverflowError,
    ProviderError,
    ToolCall,
)
from trendlab.providers.gateway import ModelGateway
from trendlab.telemetry.costs import CostTracker
from trendlab.telemetry.events import EventBus, EventType
from trendlab.tools.registry import READ_ONLY_TOOLS
from trendlab.tools.runtime import ToolRuntime

TokenCallback = Callable[[str], None]
MessageCallback = Callable[[dict[str, Any]], None]
AsyncHook = Callable[..., Awaitable[Any]]


@dataclass
class RunResult:
    status: str
    text: str
    report: str
    changed_files: list[str] = field(default_factory=list)
    validation_runs: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    model_calls: int = 0
    iterations: int = 0
    stop_reason: str | None = None
    plan: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] | None = None  # verifier verdict (spec §3.2), when it ran

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "verification": self.verification,
            "text": self.text,
            "report": self.report,
            "changed_files": self.changed_files,
            "validation": self.validation_runs,
            "cost_usd": round(self.cost_usd, 4),
            "elapsed_s": round(self.elapsed_s, 1),
            "model_calls": self.model_calls,
            "iterations": self.iterations,
            "stop_reason": self.stop_reason,
            "plan": self.plan,
        }


class AgentRuntime:
    def __init__(
        self,
        *,
        gateway: ModelGateway,
        model_ref: str,
        tools: ToolRuntime,
        context: ContextManager,
        events: EventBus,
        session_id: str,
        costs: CostTracker | None = None,
        limits: LimitsConfig | None = None,
        plan: Plan | None = None,
        role: str = "main",
        escalation_model: str | None = None,
        on_token: TokenCallback | None = None,
        on_thinking: TokenCallback | None = None,
        on_message: MessageCallback | None = None,
        hooks: Any = None,
        stream: bool = True,
    ) -> None:
        self.gateway = gateway
        self.model_ref = model_ref
        self.tools = tools
        self.context = context
        self.events = events
        self.session_id = session_id
        self.costs = costs or CostTracker(gateway.config)
        self.limits = limits or LimitsConfig()
        self.plan = plan if plan is not None else Plan()
        self.role = role
        self.escalation_model = escalation_model
        self._escalated_from: str | None = None  # model to restore when the run ends
        self.on_token = on_token
        self.on_thinking = on_thinking
        self.on_message = on_message
        self.hooks = hooks
        self.stream = stream
        self.state = AgentStateMachine(on_change=self._emit_state)
        self.evaluator = CompletionEvaluator()
        # Verify-then-surface (cheap-model spec §3.2): attached by the app.
        self.verifier: Any = None  # async (task_text, evidence) -> VerifierVerdict | None
        self.verification_mode = "off"  # required | advisory | off
        self.verification_max_rounds = 1
        self.regression_gate = False
        self._verify_rounds = 0
        self._verification: dict[str, Any] | None = None
        self._task_text = ""
        # Step-scoped execution (cheap-model spec §3.3, §4): attached by the app.
        self.planner: Any = None  # async (task_text, note) -> list[step dict] | None
        self.planner_max_calls = 3
        self.planner_min_chars = 200
        self._planner_calls = 0
        self.step_iterations = 12
        self.candidates: Any = None  # async (task, failure_tail, n) -> outcome dict
        self.best_of = 1
        self._step_id: str | None = None
        self._step_iters = 0
        self._verified_steps: set[str] = set()
        self._phase = "plan"  # phase attributed to the next model call (spec §6.2)
        self.skills: Any = None  # trendlab.extensions.skills.SkillLibrary (triggers, §6.4)
        self._skills_loaded: set[str] = set()
        self.loops = LoopDetector()
        self.iterations = 0
        self._cancel = asyncio.Event()
        self._steer: list[str] = []
        self.tools._set_state = self._tool_state_hook  # noqa: SLF001 — wiring

    # -- wiring ----------------------------------------------------------------------------
    @property
    def messages(self) -> list[dict[str, Any]]:
        return self.context.messages

    def _emit_state(self, old: AgentState, new: AgentState) -> None:
        self.events.emit(
            EventType.AGENT_STATE_CHANGED,
            session_id=self.session_id,
            old=old.value,
            new=new.value,
            role=self.role,
        )

    def _tool_state_hook(self, name: str) -> None:
        try:
            self.state.transition(AgentState(name))
        except Exception:  # noqa: BLE001 — hooks never break execution
            pass

    def _append(self, message: dict[str, Any]) -> None:
        self.context.messages.append(message)
        if self.on_message:
            self.on_message(message)

    def set_model(self, model_ref: str) -> None:
        """Hot model switch: conversation, plan, session and tool state are preserved."""
        self.model_ref = model_ref
        self._escalated_from = None  # an explicit switch is the new baseline

    def _escalate(self, failure: str) -> bool:
        """Hand the rest of this run to the stronger model (once per run); True when it happened."""
        if not self.escalation_model or self._escalated_from is not None:
            return False
        if self.escalation_model == self.model_ref:
            return False
        self.events.emit(
            EventType.RECOVERY,
            session_id=self.session_id,
            failure=failure,
            action="escalate",
            from_model=self.model_ref,
            to_model=self.escalation_model,
        )
        self._escalated_from = self.model_ref
        self.model_ref = self.escalation_model
        self.evaluator.max_nudges += 1  # the stronger model gets one more chance to finish
        return True

    def _restore_after_escalation(self) -> None:
        if self._escalated_from is not None:
            self.events.emit(
                EventType.RECOVERY,
                session_id=self.session_id,
                failure="ESCALATION",
                action="restored",
                from_model=self.model_ref,
                to_model=self._escalated_from,
            )
            self.model_ref = self._escalated_from
            self._escalated_from = None
            self.evaluator.max_nudges = max(2, self.evaluator.max_nudges - 1)

    def cancel(self) -> None:
        self._cancel.set()

    def steer(self, text: str) -> None:
        """Queue a message from the user while a run is in progress; it is delivered as the next
        user turn before the following model call (steering)."""
        text = text.strip()
        if text:
            self._steer.append(text)

    def _drain_steering(self) -> None:
        while self._steer:
            text = self._steer.pop(0)
            self._append({"role": "user", "content": text})
            self.events.emit(EventType.STEERED, session_id=self.session_id, text=text[:300])

    @property
    def validation_available(self) -> bool:
        return bool(self.tools.ctx.validation_commands)

    def evidence(self) -> EvidenceSummary:
        return EvidenceSummary(
            list(self.tools.changed_files), list(self.tools.validation_runs), self.plan
        )

    # -- main loop -------------------------------------------------------------------------
    async def run(self, prompt: str | list[dict[str, Any]]) -> RunResult:
        started = time.monotonic()
        self._cancel.clear()
        if self.state.terminal:
            self.state.transition(AgentState.IDLE)
        self._append({"role": "user", "content": prompt})
        self._task_text = _text_of(prompt)
        self._verify_rounds = 0
        self._verification = None
        self._planner_calls = 0
        self._step_id, self._step_iters = None, 0
        self._verified_steps = set()
        self._phase = "plan"
        self._skills_loaded = set()
        await self._trigger_skills(task_text=self._task_text)
        await self._maybe_plan()
        compaction_attempted = False
        stop_reason: str | None = None
        text = ""
        status = AgentState.FAILED
        # The plan is rendered into the system prompt once per run (and after a compaction), not
        # on every iteration: a byte-stable prefix is what lets DeepSeek / OpenAI / Anthropic serve
        # the system prompt + repository map from cache. Mid-run the model already sees plan
        # changes in its own task-tool results.
        refresh_plan = True
        try:
            while True:
                limit_hit = self._limit_hit(started)
                if limit_hit:
                    stop_reason, status = limit_hit, AgentState.FAILED
                    break
                if self._cancel.is_set():
                    stop_reason, status = "canceled by user", AgentState.CANCELED
                    break
                self.iterations += 1
                self._drain_steering()
                if await self._track_step():
                    refresh_plan = True
                self.state.transition(AgentState.THINKING)
                if refresh_plan:
                    self.context.plan_text = self.plan.render() if self.plan.tasks else ""
                    refresh_plan = False
                self._sync_structured()
                if self.context.needs_compaction():
                    await self.context.compact()
                    self.context.plan_text = self.plan.render() if self.plan.tasks else ""
                try:
                    response = await self._model_call()
                except ProviderContextOverflowError:
                    if compaction_attempted:
                        stop_reason, status = "context overflow after compaction", AgentState.FAILED
                        break
                    compaction_attempted = True
                    self.events.emit(
                        EventType.RECOVERY,
                        session_id=self.session_id,
                        failure=FailureClass.CONTEXT_OVERFLOW.value,
                        action="compact",
                    )
                    await self.context.compact(keep_last=4, force=True)
                    refresh_plan = True
                    self.iterations -= 1
                    continue
                except ProviderError as exc:
                    failure = classify_provider_error(exc)
                    self.events.emit(
                        EventType.RECOVERY,
                        session_id=self.session_id,
                        failure=failure.value,
                        action="stop",
                        error=str(exc)[:300],
                    )
                    stop_reason, status, text = f"{failure.value}: {exc}", AgentState.FAILED, ""
                    break

                if not response.tool_calls:
                    rescued = rescue_text_tool_calls(response.text, self.tools.registry.names())
                    if rescued:
                        # The model wrote the call as text (common with small/local models):
                        # run it instead of accepting the JSON as the final answer.
                        self._guard("tool_call_as_text")
                        self.events.emit(
                            EventType.RECOVERY,
                            session_id=self.session_id,
                            failure="TOOL_CALL_AS_TEXT",
                            action="rescued",
                            tools=[c.name for c in rescued],
                        )
                        response = response.model_copy(
                            update={
                                "tool_calls": rescued,
                                "text": prose_outside_calls(response.text)[:2000],
                            }
                        )
                if response.tool_calls:
                    self._append(_assistant_message(response))
                    if response.text and self.on_token is None:
                        pass
                    stop = await self._run_tools(response)
                    self._phase = infer_phase([c.name for c in response.tool_calls])
                    if stop:
                        stop_reason, status = stop
                        break
                    if await self._check_steps():
                        refresh_plan = True
                    await self._trigger_skills(
                        tools=[c.name for c in response.tool_calls],
                        paths=[
                            str(c.arguments.get("path") or "")
                            for c in response.tool_calls
                            if c.arguments.get("path")
                        ],
                    )
                    continue

                # Final text: let the evaluator decide.
                self.state.transition(AgentState.VALIDATING)
                verdict = self.evaluator.evaluate(
                    self.evidence(),
                    response.text,
                    validation_available=self.validation_available,
                    fix_task=self.regression_gate and looks_like_fix(self._task_text),
                )
                self._append({"role": "assistant", "content": response.text})
                for reason in verdict.reasons if not verdict.accept else []:
                    self._guard(_guard_name(reason))
                if verdict.accept:
                    outcome = await self._verify_before_surface(response.text)
                    if outcome is None:
                        text, status = response.text, AgentState.COMPLETED
                        break
                    kind, payload = outcome
                    if kind == "fix":
                        self._append({"role": "user", "content": payload})
                        continue
                    text, status, stop_reason = response.text, AgentState.FAILED, payload
                    break
                self.events.emit(
                    EventType.RECOVERY,
                    session_id=self.session_id,
                    failure="UNVERIFIED_COMPLETION",
                    action="feedback",
                    reasons=verdict.reasons,
                )
                if self.evaluator.nudges >= 2:
                    # Two rejections in a row: a quality problem, not a slip. Let the stronger
                    # model take the rest of this run (restored when the run ends).
                    self._escalate("UNVERIFIED_COMPLETION")
                self._append({"role": "user", "content": verdict.nudge})
        except asyncio.CancelledError:
            status, stop_reason = AgentState.CANCELED, "canceled"
        self.state.transition(status)
        self._restore_after_escalation()
        for name in sorted(self._skills_loaded):
            self.events.emit(EventType.SKILL_UNLOADED, session_id=self.session_id, name=name)
        return self._finish(status, text, stop_reason, started)

    def _limit_hit(self, started: float) -> str | None:
        if self.iterations >= self.limits.max_iterations:
            return f"max_iterations ({self.limits.max_iterations}) reached"
        if self.costs.over_budget():
            self.events.emit(
                EventType.BUDGET_EXCEEDED,
                session_id=self.session_id,
                total_usd=round(self.costs.total_usd, 4),
                limit=self.limits.max_cost_usd,
            )
            return f"cost limit ${self.limits.max_cost_usd:.2f} reached"
        if self.costs.over_call_limit():
            return f"max_model_calls ({self.limits.max_model_calls}) reached"
        if self.limits.max_wall_clock_minutes and (
            time.monotonic() - started > self.limits.max_wall_clock_minutes * 60
        ):
            return f"wall-clock limit ({self.limits.max_wall_clock_minutes} min) reached"
        return None

    def _sync_structured(self) -> None:
        self.context.structured = {
            "changed_files": list(self.tools.changed_files),
            "validation_runs": self.tools.validation_runs[-5:],
            "plan": self.plan.render() if self.plan.tasks else "(none)",
        }

    async def _model_call(self) -> ModelResponse:
        messages = self.context.build()
        tools = self.tools.registry.schemas()
        if self.hooks is not None:
            await self.hooks.run("before_model_call", model=self.model_ref)
        self.events.emit(
            EventType.MODEL_CALL_STARTED,
            session_id=self.session_id,
            model=self.model_ref,
            role=self.role,
            messages=len(messages),
            est_tokens=self.context.estimate(),
        )
        t0 = time.monotonic()
        used = self.model_ref
        if self.stream and self.on_token is not None:
            response: ModelResponse | None = None
            async for chunk in self.gateway.stream(self.model_ref, messages, tools):
                if chunk.thinking and self.on_thinking is not None:
                    self.on_thinking(chunk.thinking)
                if chunk.text:
                    self.on_token(chunk.text)
                if chunk.final is not None:
                    response = chunk.final
            if response is None:
                response = ModelResponse(text="")
        else:
            response, used = await self.gateway.complete(self.model_ref, messages, tools)
        latency = int((time.monotonic() - t0) * 1000)
        caps = self.gateway.provider(used).capabilities()
        active = self.plan.active
        rec = self.costs.record(
            used,
            response.usage,
            latency,
            role=self.role,
            local=caps.local,
            phase=self._phase,
            step_id=active.id if active else None,
            attempt=(active.attempts + 1) if active else 0,
        )
        self._phase = "other"
        self.events.emit(
            EventType.MODEL_CALL_COMPLETED,
            session_id=self.session_id,
            model=used,
            role=self.role,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cached_input_tokens=response.usage.cached_input_tokens,
            latency_ms=latency,
            cost_usd=round(rec.cost_usd, 6),
            tool_calls=len(response.tool_calls),
            privacy=caps.privacy_label,
        )
        self.events.emit(
            EventType.COST_UPDATED,
            session_id=self.session_id,
            total_usd=round(self.costs.total_usd, 6),
            model_calls=self.costs.model_calls,
        )
        if self.costs.should_warn():
            self.events.emit(
                EventType.BUDGET_WARNING,
                session_id=self.session_id,
                total_usd=round(self.costs.total_usd, 4),
                limit=self.limits.max_cost_usd,
            )
        if self.hooks is not None:
            await self.hooks.run("after_model_call", model=used, cost_usd=rec.cost_usd)
        return response

    async def _run_tools(self, response: ModelResponse) -> tuple[str, AgentState] | None:
        """Execute the model's tool calls. Runs of consecutive read-only calls execute
        concurrently (bounded by limits.parallel_tools); anything else runs in order."""
        calls = list(response.tool_calls)
        i = 0
        while i < len(calls):
            if self._cancel.is_set():
                for c in calls[i:]:
                    self._append(_tool_message(c, "NOT EXECUTED: run canceled by user"))
                return "canceled by user", AgentState.CANCELED
            self.state.transition(AgentState.RUNNING_TOOL)
            group = [calls[i]]
            if self._is_read_only(calls[i]):
                while (
                    i + len(group) < len(calls)
                    and len(group) < self.limits.parallel_tools
                    and self._is_read_only(calls[i + len(group)])
                ):
                    group.append(calls[i + len(group)])
            if len(group) == 1:
                results = [await self.tools.execute(group[0])]
            else:
                self.events.emit(
                    EventType.TOOLS_PARALLEL,
                    session_id=self.session_id,
                    count=len(group),
                    tools=[c.name for c in group],
                )
                results = list(await asyncio.gather(*(self.tools.execute(c) for c in group)))
            for call, result in zip(group, results, strict=True):
                stop = self._observe(call, result)
                if stop:
                    return stop
            i += len(group)
        return None

    def _is_read_only(self, call) -> bool:
        return call.name in READ_ONLY_TOOLS

    def _observe(self, call, result) -> tuple[str, AgentState] | None:
        output = result.output
        if result.data.get("plan_rejected"):
            self._append(_tool_message(call, output))
            return f"plan rejected by user: {result.data.get('reason', '')}", AgentState.FAILED
        fingerprint = json.dumps(call.arguments, sort_keys=True, default=str)
        reason = self.loops.record(call.name, fingerprint, output, result.ok)
        if reason:
            self._guard("no_progress")
            action = recovery_for(
                FailureClass.NO_PROGRESS,
                compaction_attempted=False,
                escalation_available=bool(self.escalation_model),
                no_progress_strikes=self.loops.strikes,
            )
            self.events.emit(
                EventType.LOOP_DETECTED,
                session_id=self.session_id,
                tool=call.name,
                reason=reason,
                strikes=self.loops.strikes,
                action=action.value,
            )
            if action == RecoveryAction.ESCALATE and self.escalation_model:
                if not self._escalate(FailureClass.NO_PROGRESS.value):
                    self._append(_tool_message(call, output))
                    return f"NO_PROGRESS: {reason}", AgentState.FAILED
            elif action == RecoveryAction.STOP:
                self._append(_tool_message(call, output))
                return f"NO_PROGRESS: {reason}", AgentState.FAILED
            output += "\n\n" + NO_PROGRESS_FEEDBACK.format(reason=reason)
        self._append(_tool_message(call, output))
        return None

    # -- telemetry helpers (spec §6.2, §6.3) -------------------------------------------------
    def _guard(self, guard: str) -> None:
        """A weak-model guard fired; counted per model so useless guards can be retired."""
        self.events.emit(
            EventType.GUARD_FIRED,
            session_id=self.session_id,
            guard=guard,
            model=self.model_ref,
            role=self.role,
        )

    async def _trigger_skills(
        self,
        *,
        tools: list[str] | None = None,
        paths: list[str] | None = None,
        failure: str | None = None,
        task_text: str | None = None,
    ) -> None:
        """Skill triggers (§6.4): a matching skill joins the run as a user message once."""
        if self.skills is None:
            return
        try:
            matches = self.skills.match(
                task_text=task_text, tools=tools, paths=paths, failure=failure
            )
        except Exception:  # noqa: BLE001
            return
        for skill, trigger in matches:
            if skill.name in self._skills_loaded:
                continue
            self._skills_loaded.add(skill.name)
            self.events.emit(
                EventType.SKILL_LOADED,
                session_id=self.session_id,
                name=skill.name,
                trigger=trigger,
            )
            self._append(
                {
                    "role": "user",
                    "content": f"Skill '{skill.name}' applies here ({trigger}). Follow it:\n"
                    + skill.instructions[:6000],
                }
            )

    # -- step-scoped execution (spec §3.3, §4) ---------------------------------------------
    async def _maybe_plan(self, note: str = "") -> bool:
        """One planner call for a non-trivial task (or a re-plan after a failed step)."""
        from trendlab.agent.planner import apply_steps, needs_planner, plan_message

        if self.planner is None or self._planner_calls >= self.planner_max_calls:
            return False
        if not note and (
            self.plan.open
            or not needs_planner(self._task_text, min_prompt_chars=self.planner_min_chars)
        ):
            return False
        self._planner_calls += 1
        try:
            steps = await self.planner(self._task_text, note)
        except Exception as exc:  # noqa: BLE001 — planning is best effort
            self.events.emit(
                EventType.PLANNER_CALLED,
                session_id=self.session_id,
                call=self._planner_calls,
                steps=0,
                error=str(exc)[:200],
            )
            return False
        if not steps:
            self.events.emit(
                EventType.PLANNER_CALLED,
                session_id=self.session_id,
                call=self._planner_calls,
                steps=0,
            )
            return False
        added = apply_steps(self.plan, steps)
        self.events.emit(
            EventType.PLANNER_CALLED,
            session_id=self.session_id,
            call=self._planner_calls,
            steps=len(added),
            titles=[t.title for t in added],
            replan=bool(note),
        )
        self._append({"role": "user", "content": plan_message(added)})
        return True

    async def _track_step(self) -> bool:
        """Per-step iteration budget (§4.1). Returns True when the plan text must refresh."""
        active = self.plan.active
        if active is None:
            self._step_id = None
            return False
        if active.id != self._step_id:
            self._step_id, self._step_iters = active.id, 1
            self.events.emit(
                EventType.STEP_STARTED,
                session_id=self.session_id,
                step=active.id,
                title=active.title,
                attempt=active.attempts + 1,
            )
            return False
        self._step_iters += 1
        if self._step_iters <= self.step_iterations:
            return False
        active.attempts += 1
        self._step_iters = 0
        self._guard("step_iteration_cap")
        self.events.emit(
            EventType.STEP_FAILED,
            session_id=self.session_id,
            step=active.id,
            title=active.title,
            attempt=active.attempts,
            reason="iteration_cap",
            cap=self.step_iterations,
        )
        note = (
            f"Step {active.id} ({active.title}) did not finish within {self.step_iterations} "
            f"iterations (attempt {active.attempts}). Re-plan the remaining work in smaller steps."
        )
        if await self._maybe_plan(note):
            return True
        if active.attempts >= 2 and self._escalate("STEP_STALLED"):
            return False
        self._append(
            {
                "role": "user",
                "content": f"Step {active.id} is taking too long ({self.step_iterations} "
                "iterations). Narrow it: do the single smallest change that moves it forward, "
                "validate, and complete or fail the step with a reason.",
            }
        )
        return False

    async def _check_steps(self) -> bool:
        """Run the validation of steps the model just marked complete (§4.1); on failure hand
        the step back, and after the first failed attempt try best-of-N candidates (§4.2)."""
        changed = False
        for task in list(self.plan.tasks):
            if (
                task.status != TaskStatus.COMPLETED
                or not task.validation
                or task.id in self._verified_steps
            ):
                continue
            result = await self.tools.execute(
                ToolCall(
                    id=f"step-{task.id}-v{task.attempts + 1}",
                    name="shell",
                    arguments={"command": task.validation},
                )
            )
            if result.ok:
                self._verified_steps.add(task.id)
                self.events.emit(
                    EventType.STEP_COMPLETED,
                    session_id=self.session_id,
                    step=task.id,
                    title=task.title,
                    attempt=task.attempts + 1,
                    validation=task.validation,
                )
                continue
            task.attempts += 1
            self.plan.update(task.id, status=TaskStatus.ACTIVE)
            changed = True
            tail = (result.output or "")[-1500:]
            self.events.emit(
                EventType.STEP_FAILED,
                session_id=self.session_id,
                step=task.id,
                title=task.title,
                attempt=task.attempts,
                reason="validation_failed",
                validation=task.validation,
                tail=tail[-400:],
            )
            await self._trigger_skills(failure=tail)
            if self.candidates is not None and self.best_of > 1 and task.attempts == 1:
                outcome = await self._best_of(task, tail)
                if outcome:
                    continue
            if task.attempts >= 2:
                self._escalate("STEP_FAILED")
            self._append(
                {
                    "role": "user",
                    "content": f"Step {task.id} is not done: its validation `{task.validation}` "
                    f"failed (attempt {task.attempts}):\n{tail}\nFix the cause, then complete "
                    "the step again; or fail it with a reason if it cannot be done.",
                }
            )
        return changed

    async def _best_of(self, task: Task, tail: str) -> bool:
        try:
            outcome = await self.candidates(task, tail, self.best_of)
        except Exception as exc:  # noqa: BLE001 — candidates are best effort
            outcome = {"applied": False, "reason": str(exc)[:200]}
        if not outcome or not outcome.get("applied"):
            self.events.emit(
                EventType.STEP_FAILED,
                session_id=self.session_id,
                step=task.id,
                title=task.title,
                attempt=task.attempts,
                reason="best_of_exhausted",
                detail=(outcome or {}).get("reason", ""),
                candidates=(outcome or {}).get("candidates", []),
            )
            return False
        for f in outcome.get("files") or []:
            self.tools.changed_files.setdefault(f, []).append("")
        self.plan.update(
            task.id,
            status=TaskStatus.COMPLETED,
            evidence=f"best-of-{self.best_of}: candidate {outcome.get('winner')} passed "
            f"`{task.validation}`",
        )
        self._verified_steps.add(task.id)
        self.events.emit(
            EventType.STEP_COMPLETED,
            session_id=self.session_id,
            step=task.id,
            title=task.title,
            attempt=task.attempts + 1,
            via="best_of",
            winner=outcome.get("winner"),
            candidates=outcome.get("candidates", []),
        )
        nxt = next((t for t in self.plan.open if t.status == TaskStatus.PENDING), None)
        if nxt and self.plan.active is None:
            self.plan.update(nxt.id, status=TaskStatus.ACTIVE)
        self._append(
            {
                "role": "user",
                "content": f"Step {task.id} was finished by a parallel attempt: candidate "
                f"{outcome.get('winner')} passed `{task.validation}` and its diff is now in the "
                "working tree (files: " + ", ".join(outcome.get("files") or []) + "). Re-read "
                "those files before touching them. Continue with the next step.",
            }
        )
        return True

    async def _verify_before_surface(self, final_text: str) -> tuple[str, str] | None:
        """Independent review of the diff (spec §3.2). None = surface the run as COMPLETED;
        ("fix", feedback) = one more author round; ("fail", reason) = stop the run."""
        ev = self.evidence()
        if self.regression_gate and ev.mutated and looks_like_fix(self._task_text):
            outcome = regression_outcome(ev.changed_files, final_text, ev.validation_runs)
            self.events.emit(
                EventType.REGRESSION_GATE,
                session_id=self.session_id,
                outcome=outcome,
                files=ev.changed_files,
            )
        if self.verification_mode == "off" or self.verifier is None or not ev.mutated:
            return None
        self.events.emit(EventType.VERIFY_STARTED, session_id=self.session_id, role=self.role)
        try:
            verdict = await self.verifier(self._task_text, ev)
        except Exception as exc:  # noqa: BLE001 — a broken verifier never blocks surfacing
            verdict = None
            self.events.emit(
                EventType.VERIFY_VERDICT,
                session_id=self.session_id,
                verdict="unavailable",
                error=str(exc)[:200],
            )
        if verdict is None:
            self._verification = {"verdict": "unavailable"}
            return None
        self._verification = verdict.to_json()
        self.events.emit(
            EventType.VERIFY_VERDICT,
            session_id=self.session_id,
            verdict=verdict.verdict,
            findings=len(verdict.findings),
            regression_test=verdict.regression_test,
            model=verdict.model,
            mode=self.verification_mode,
            detail=verdict.findings[:3],
        )
        if verdict.verdict == "pass":
            return None
        if verdict.verdict == "fix" and self._verify_rounds < self.verification_max_rounds:
            self._verify_rounds += 1
            self._guard("verifier_fix_round")
            self.evaluator.nudges = 0  # the fix round gets a fresh evaluator budget
            return ("fix", verdict.feedback())
        if self.verification_mode == "advisory" or verdict.verdict == "fix":
            # 'fix' means addressable findings, not a wrong change: once the author's round is
            # used up the run surfaces with the findings attached rather than failing.
            return None
        first = verdict.findings[0]["issue"] if verdict.findings else "no details given"
        return ("fail", f"verifier rejected the change: {first}")

    def _finish(
        self, status: AgentState, text: str, stop_reason: str | None, started: float
    ) -> RunResult:
        elapsed = time.monotonic() - started
        ev = self.evidence()
        body = text if status == AgentState.COMPLETED else (stop_reason or "")
        report = final_report(
            ev,
            body,
            cost_usd=self.costs.total_usd,
            elapsed_s=elapsed,
            status=status.value,
            model_calls=self.costs.model_calls,
        )
        result = RunResult(
            verification=self._verification,
            status=status.value,
            text=text,
            report=report,
            changed_files=ev.changed_files,
            validation_runs=ev.validation_runs,
            cost_usd=self.costs.total_usd,
            elapsed_s=elapsed,
            model_calls=self.costs.model_calls,
            iterations=self.iterations,
            stop_reason=stop_reason,
            plan=self.plan.to_json(),
        )
        event = {
            AgentState.COMPLETED: EventType.RUN_COMPLETED,
            AgentState.CANCELED: EventType.RUN_CANCELED,
        }.get(status, EventType.RUN_FAILED)
        self.events.emit(
            event,
            session_id=self.session_id,
            role=self.role,
            stop_reason=stop_reason,
            text=(text or "")[:3000],
            changed_files=ev.changed_files,
            validated=ev.validated,
            cost_usd=round(self.costs.total_usd, 4),
            elapsed_s=round(elapsed, 1),
            iterations=self.iterations,
        )
        return result


def _assistant_message(response: ModelResponse) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": response.text or None,
    }
    if response.raw_metadata.get("provider_content"):
        # Providers with richer turns (e.g. Anthropic thinking blocks) replay these verbatim
        # when the same model continues; other providers ignore underscore keys.
        message["_provider_content"] = response.raw_metadata["provider_content"]
        message["_provider_model"] = response.raw_metadata.get("provider_model")
    if response.raw_metadata.get("reasoning_content"):
        message["_reasoning_content"] = response.raw_metadata["reasoning_content"]
        message["_provider_model"] = response.raw_metadata.get("provider_model")
    message["tool_calls"] = [
        {
            "id": c.id,
            "type": "function",
            "function": {"name": c.name, "arguments": json.dumps(c.arguments, default=str)},
        }
        for c in response.tool_calls
    ]
    return message


def _tool_message(call, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content}


def _text_of(prompt: str | list[dict[str, Any]]) -> str:
    if isinstance(prompt, str):
        return prompt
    return " ".join(str(p.get("text") or "") for p in prompt if isinstance(p, dict))


_FIX_WORDS = re.compile(
    r"\b(fix|bug|broken|breaks?|fail(?:s|ing|ed|ure)?|error|crash(?:es|ed)?|regress(?:ion|ed)?|"
    r"wrong|incorrect|does ?n[o']t work|not working|exception|traceback)\b",
    re.I,
)
_WAIVER = re.compile(r"regression test:?\s*not applicable", re.I)
_TEST_PATH = re.compile(
    r"(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$|"
    r"(^|/)conftest\.py$",
    re.I,
)


def looks_like_fix(task_text: str) -> bool:
    """Heuristic: the task reads as a bug fix (spec §5.2), so a regression test is expected."""
    return bool(_FIX_WORDS.search(task_text or ""))


def is_test_file(path: str) -> bool:
    return bool(_TEST_PATH.search(path.replace("\\", "/")))


def regression_outcome(
    changed_files: list[str], final_text: str, validation_runs: list[dict[str, Any]] | None = None
) -> str:
    """present | waived | missing | not_applicable (only tests or docs changed).

    "present" means a test change was made, or the existing suite failed before the fix and
    passed after it (the test that catches the bug already existed)."""
    source = [f for f in changed_files if not is_test_file(f)]
    if not source:
        return "not_applicable"
    if any(is_test_file(f) for f in changed_files):
        return "present"
    oks = [bool(r.get("ok")) for r in (validation_runs or [])]
    if False in oks and oks[-1] and oks.index(False) < len(oks) - 1:
        return "present"
    if _WAIVER.search(final_text or ""):
        return "waived"
    return "missing"


_EXPLORE_TOOLS = {
    "read_file",
    "list_directory",
    "glob",
    "search_text",
    "git_status",
    "git_diff",
    "git_log",
    "inspect_output",
    "web_fetch",
    "web_search",
    "delegate",
}
_EDIT_TOOLS = {"write_file", "patch_file", "apply_patch", "delete_file"}
_VALIDATE_TOOLS = {"run_tests", "shell", "background"}


def infer_phase(tools: list[str]) -> str:
    """Phase of the next model call from the tools it is reacting to (spec §6.2)."""
    names = set(tools)
    if names & _EDIT_TOOLS:
        return "edit"
    if names & _VALIDATE_TOOLS:
        return "validate"
    if "task" in names:
        return "plan"
    if names & _EXPLORE_TOOLS:
        return "explore"
    return "other"


def _guard_name(reason: str) -> str:
    r = reason.lower()
    if "empty" in r or "json" in r:
        return "empty_answer"
    if "announced" in r:
        return "announced_action"
    if "regression test" in r:
        return "regression_test_missing"
    if "validation" in r:
        return "unvalidated_change"
    if "plan tasks" in r:
        return "open_tasks"
    return "evaluator_other"
