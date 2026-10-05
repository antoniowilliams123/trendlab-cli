"""The agent loop (spec §12): context → model → tools → observe → evaluate → repeat.

Permission waits happen *inside* a tool call, so a remote approval pauses only
that operation while session and conversation state stay intact. Completion is
decided by the evaluator from evidence, not by the model saying "done".
"""

from __future__ import annotations

import asyncio
import json
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
from trendlab.agent.state import AgentState, AgentStateMachine
from trendlab.agent.tasks import Plan
from trendlab.config.schema import LimitsConfig
from trendlab.context.manager import ContextManager
from trendlab.providers.base import ModelResponse, ProviderContextOverflowError, ProviderError
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

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
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
        self.on_token = on_token
        self.on_thinking = on_thinking
        self.on_message = on_message
        self.hooks = hooks
        self.stream = stream
        self.state = AgentStateMachine(on_change=self._emit_state)
        self.evaluator = CompletionEvaluator()
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

    def cancel(self) -> None:
        self._cancel.set()

    def steer(self, text: str) -> None:
        """Queue a message from the user while a run is in progress; it is delivered as the next
        user turn before the following model call (Claude-Code-style steering)."""
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
        compaction_attempted = False
        stop_reason: str | None = None
        text = ""
        status = AgentState.FAILED
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
                self.state.transition(AgentState.THINKING)
                self.context.plan_text = self.plan.render() if self.plan.tasks else ""
                self._sync_structured()
                if self.context.needs_compaction():
                    await self.context.compact()
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

                if response.tool_calls:
                    self._append(_assistant_message(response))
                    if response.text and self.on_token is None:
                        pass
                    stop = await self._run_tools(response)
                    if stop:
                        stop_reason, status = stop
                        break
                    continue

                # Final text: let the evaluator decide.
                self.state.transition(AgentState.VALIDATING)
                verdict = self.evaluator.evaluate(
                    self.evidence(), response.text, validation_available=self.validation_available
                )
                self._append({"role": "assistant", "content": response.text})
                if verdict.accept:
                    text, status = response.text, AgentState.COMPLETED
                    break
                self.events.emit(
                    EventType.RECOVERY,
                    session_id=self.session_id,
                    failure="UNVERIFIED_COMPLETION",
                    action="feedback",
                    reasons=verdict.reasons,
                )
                self._append({"role": "user", "content": verdict.nudge})
        except asyncio.CancelledError:
            status, stop_reason = AgentState.CANCELED, "canceled"
        self.state.transition(status)
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
        rec = self.costs.record(used, response.usage, latency, role=self.role, local=caps.local)
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
        fingerprint = json.dumps(call.arguments, sort_keys=True, default=str)
        reason = self.loops.record(call.name, fingerprint, output, result.ok)
        if reason:
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
                self.events.emit(
                    EventType.RECOVERY,
                    session_id=self.session_id,
                    failure=FailureClass.NO_PROGRESS.value,
                    action="escalate",
                    from_model=self.model_ref,
                    to_model=self.escalation_model,
                )
                self.model_ref = self.escalation_model
                self.escalation_model = None
            elif action == RecoveryAction.STOP:
                self._append(_tool_message(call, output))
                return f"NO_PROGRESS: {reason}", AgentState.FAILED
            output += "\n\n" + NO_PROGRESS_FEEDBACK.format(reason=reason)
        self._append(_tool_message(call, output))
        return None

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
