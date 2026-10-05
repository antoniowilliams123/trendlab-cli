"""ApprovalManager — resolves the permission engine's ASK verdicts.

Flow:  PermissionEngine → ASK → ApprovalManager.request()
         → persist (session state) → emit approval.requested
         → dispatch to channels (local always; remote only when enabled+allowed)
         → notify (separately; failure never blocks)
         → await the first valid decision, or expiry
         → emit approved/denied/expired, return DecisionResult

Decisions arrive via ``decide()`` from any thread (HTTP handler threads call it
directly). All validation lives here so no channel can bypass it:
  * unknown id → 404      * already decided (replay) → 409
  * expired → 410         * bad decision token → 403
  * fingerprint mismatch (operation changed) → 409
  * remote decision on a local-only request → 403
  * session scope where not permitted → 403
"""

from __future__ import annotations

import asyncio
import hmac
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from trendlab.approvals.channels.base import ApprovalChannel, ChannelError
from trendlab.approvals.models import (
    ApprovalDecision,
    ApprovalError,
    ApprovalRequest,
    ApprovalScope,
    ApprovalStatus,
    DecisionResult,
)
from trendlab.approvals.notifications.base import (
    NotificationError,
    NotificationProvider,
    build_notification,
)
from trendlab.config.schema import RemoteApprovalConfig
from trendlab.permissions.engine import PermissionRequest
from trendlab.permissions.models import OperationCategory, RiskLevel
from trendlab.sessions.store import SessionStore
from trendlab.telemetry.events import EventBus, EventType

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _Pending:
    __slots__ = ("request", "future", "loop")

    def __init__(
        self, request: ApprovalRequest, future: asyncio.Future, loop: asyncio.AbstractEventLoop
    ):
        self.request = request
        self.future = future
        self.loop = loop


class ApprovalManager:
    def __init__(
        self,
        *,
        config: RemoteApprovalConfig,
        events: EventBus,
        store: SessionStore,
        session_id: str,
        machine: str,
        project_name: str,
        notifier: NotificationProvider | None = None,
        approval_url: str | None = None,
        clock: Clock = _utcnow,
        process_id: str | None = None,
    ) -> None:
        self.config = config
        self.events = events
        self.store = store
        self.session_id = session_id
        self.machine = machine
        self.project_name = project_name
        self.notifier = notifier
        self.approval_url = approval_url
        self.clock = clock
        self.process_id = process_id or uuid.uuid4().hex
        self._channels: list[ApprovalChannel] = []
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.RLock()
        self._on_pending_changed: list[Callable[[], None]] = []
        self._reminded: set[str] = set()

    # -- channels ---------------------------------------------------------------
    @property
    def channels(self) -> list[ApprovalChannel]:
        return list(self._channels)

    async def add_channel(self, channel: ApprovalChannel) -> None:
        await channel.start(self)
        with self._lock:
            self._channels.append(channel)

    async def remove_channel(self, channel: ApprovalChannel) -> None:
        with self._lock:
            if channel in self._channels:
                self._channels.remove(channel)
        await channel.stop()

    def remote_channel(self) -> ApprovalChannel | None:
        return next((c for c in self._channels if c.remote), None)

    @property
    def remote_enabled(self) -> bool:
        return self.config.enabled and self.remote_channel() is not None

    # -- queries ------------------------------------------------------------------
    def pending(self, *, remote_only: bool = False) -> list[ApprovalRequest]:
        self.sweep_expired()
        with self._lock:
            items = [p.request for p in self._pending.values()]
        if remote_only:
            items = [r for r in items if r.remote_allowed]
        return sorted(items, key=lambda r: r.created_at)

    def get(self, approval_id: str) -> ApprovalRequest | None:
        with self._lock:
            p = self._pending.get(approval_id)
        return p.request if p else None

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.approvals(session_id=self.session_id, limit=limit)

    def on_pending_changed(self, fn: Callable[[], None]) -> None:
        self._on_pending_changed.append(fn)

    def _notify_pending_changed(self) -> None:
        for fn in list(self._on_pending_changed):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

    # -- request lifecycle ----------------------------------------------------------
    def _remote_allowed_for(self, req: PermissionRequest) -> bool:
        if req.risk == RiskLevel.HIGH and not self.config.allow_high_risk:
            return False
        return True

    async def request(
        self,
        perm_req: PermissionRequest,
        *,
        timeout: timedelta | None = None,
        can_persist: bool = True,
    ) -> DecisionResult:
        """Create an approval request, present it, and wait for the outcome."""
        timeout = timeout or timedelta(minutes=self.config.request_timeout_minutes)
        remote_allowed = self._remote_allowed_for(perm_req)
        session_scope_allowed = can_persist and self.config.allow_session_scope
        request = ApprovalRequest.from_permission_request(
            perm_req,
            session_id=self.session_id,
            machine=self.machine,
            timeout=timeout,
            remote_allowed=remote_allowed,
            session_scope_allowed=session_scope_allowed,
        )
        # Clock may be injected for tests; keep the model's timestamps consistent with it.
        now = self.clock()
        request.created_at = now
        request.expires_at = now + timeout

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        with self._lock:
            # A newer request for the same operation supersedes an older pending one.
            for other in list(self._pending.values()):
                if other.request.fingerprint == request.fingerprint:
                    self._resolve_locked(
                        other, ApprovalStatus.SUPERSEDED, reason="superseded by newer request"
                    )
            self._pending[request.approval_id] = _Pending(request, future, loop)
        self.store.insert_approval(request.to_row(self.process_id))
        self.events.emit(
            EventType.APPROVAL_REQUESTED,
            session_id=self.session_id,
            **self._event_fields(request),
            remote_enabled=self.remote_enabled,
        )
        self._notify_pending_changed()

        await self._dispatch(request)

        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout.total_seconds())
        except TimeoutError:
            return self._expire(request.approval_id)
        except asyncio.CancelledError:
            self.cancel(request.approval_id, "run canceled")
            raise
        finally:
            with self._lock:
                self._pending.pop(request.approval_id, None)
            self._notify_pending_changed()

    async def ask(
        self,
        question: str,
        options: list[str] | None = None,
        *,
        task_id: str | None = None,
        timeout: timedelta | None = None,
    ) -> DecisionResult:
        """Ask the human a free-text question through the same channels. Answer is in ``reason``."""
        timeout = timeout or timedelta(minutes=self.config.request_timeout_minutes)
        perm = PermissionRequest(
            tool="ask_user",
            category=OperationCategory.READ_ONLY,
            summary=question[:200],
            cwd="",
            task_id=task_id,
            args={"q": question},
        )
        request = ApprovalRequest.from_permission_request(
            perm,
            session_id=self.session_id,
            machine=self.machine,
            timeout=timeout,
            remote_allowed=True,
            session_scope_allowed=False,
        )
        request.kind = "question"
        request.options = list(options or [])[:8]
        request.explanation = question
        now = self.clock()
        request.created_at, request.expires_at = now, now + timeout
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        with self._lock:
            self._pending[request.approval_id] = _Pending(request, future, loop)
        self.store.insert_approval(request.to_row(self.process_id))
        self.events.emit(
            EventType.QUESTION_ASKED,
            session_id=self.session_id,
            approval_id=request.approval_id,
            question=question[:300],
            options=request.options,
            task_id=task_id,
        )
        self._notify_pending_changed()
        await self._dispatch(request)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout.total_seconds())
        except TimeoutError:
            return self._expire(request.approval_id)
        except asyncio.CancelledError:
            self.cancel(request.approval_id, "run canceled")
            raise
        finally:
            with self._lock:
                self._pending.pop(request.approval_id, None)
            self._notify_pending_changed()

    def answer(
        self,
        approval_id: str,
        text: str,
        *,
        via: str,
        by: str | None = None,
        decision_token: str | None = None,
        trusted: bool = False,
    ) -> DecisionResult:
        """Answer a pending question. Validated like a decision (token, expiry, single use)."""
        text = (text or "").strip()
        if not text:
            raise ApprovalError("invalid_answer", "answer must not be empty", 400)
        with self._lock:
            pending = self._pending.get(approval_id)
            if pending is None:
                row = self.store.get_approval(approval_id)
                if row is None:
                    self._reject(approval_id, via, by, "unknown")
                    raise ApprovalError("unknown", "unknown question id", 404)
                self._reject(approval_id, via, by, "already_decided")
                raise ApprovalError("already_decided", f"question already {row['status']}", 409)
            request = pending.request
            if request.kind != "question":
                raise ApprovalError("not_a_question", "this id is an approval, not a question", 400)
            if request.is_expired(self.clock()):
                self._resolve_locked(pending, ApprovalStatus.EXPIRED, reason="expired")
                raise ApprovalError("expired", "question expired", 410)
            if not trusted and (
                not decision_token
                or not hmac.compare_digest(decision_token, request.decision_token)
            ):
                self._reject(approval_id, via, by, "invalid_token")
                raise ApprovalError("invalid_token", "invalid decision token", 403)
            self.events.emit(
                EventType.QUESTION_ANSWERED,
                session_id=self.session_id,
                approval_id=approval_id,
                channel=via,
                client=by,
                answer=text[:500],
            )
            result = self._resolve_locked(
                pending, ApprovalStatus.APPROVED, via=via, by=by, reason=text[:4000]
            )
        self._notify_pending_changed()
        return result

    async def send_reminders(self, fraction: float) -> list[str]:
        """Re-notify for pending remote-eligible requests past ``fraction`` of their lifetime."""
        if self.notifier is None or not self.config.enabled or fraction <= 0:
            return []
        now = self.clock()
        reminded: list[str] = []
        for req in self.pending(remote_only=True):
            if req.approval_id in self._reminded:
                continue
            life = (req.expires_at - req.created_at).total_seconds() or 1
            if (now - req.created_at).total_seconds() / life >= fraction:
                self._reminded.add(req.approval_id)
                notification = build_notification(req, self.approval_url, self.project_name)
                notification.title = notification.title.replace(
                    "Approval Required", "Reminder — still waiting"
                )
                try:
                    await self.notifier.send(notification)
                    reminded.append(req.approval_id)
                    self.events.emit(
                        EventType.APPROVAL_DISPATCHED,
                        session_id=self.session_id,
                        approval_id=req.approval_id,
                        channel=f"notify:{self.notifier.name}",
                        reminder=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    self.events.emit(
                        EventType.APPROVAL_DELIVERY_FAILED,
                        session_id=self.session_id,
                        approval_id=req.approval_id,
                        channel=f"notify:{self.notifier.name}",
                        error=str(exc)[:200],
                        reminder=True,
                    )
        return reminded

    async def _dispatch(self, request: ApprovalRequest) -> None:
        for channel in self.channels:
            if channel.remote and not (self.config.enabled and request.remote_allowed):
                continue
            try:
                await channel.dispatch(request)
                self.events.emit(
                    EventType.APPROVAL_DISPATCHED,
                    session_id=self.session_id,
                    approval_id=request.approval_id,
                    channel=channel.name,
                )
            except ChannelError as exc:
                self.events.emit(
                    EventType.APPROVAL_DELIVERY_FAILED,
                    session_id=self.session_id,
                    approval_id=request.approval_id,
                    channel=channel.name,
                    error=str(exc),
                )
        if self.notifier is not None and self.config.enabled:
            notification = build_notification(request, self.approval_url, self.project_name)
            try:
                await self.notifier.send(notification)
                self.events.emit(
                    EventType.APPROVAL_DISPATCHED,
                    session_id=self.session_id,
                    approval_id=request.approval_id,
                    channel=f"notify:{self.notifier.name}",
                )
            except NotificationError as exc:
                self.events.emit(
                    EventType.APPROVAL_DELIVERY_FAILED,
                    session_id=self.session_id,
                    approval_id=request.approval_id,
                    channel=f"notify:{self.notifier.name}",
                    error=str(exc),
                )
            except Exception as exc:  # noqa: BLE001 — provider bug must not block approval
                self.events.emit(
                    EventType.APPROVAL_DELIVERY_FAILED,
                    session_id=self.session_id,
                    approval_id=request.approval_id,
                    channel=f"notify:{self.notifier.name}",
                    error=f"{exc.__class__.__name__}: {exc}",
                )

    # -- decisions ------------------------------------------------------------------
    def decide(
        self,
        approval_id: str,
        decision: ApprovalDecision | str,
        scope: ApprovalScope | str = ApprovalScope.ONCE,
        *,
        via: str,
        by: str | None = None,
        decision_token: str | None = None,
        fingerprint: str | None = None,
        trusted: bool = False,
    ) -> DecisionResult:
        """Apply a decision. Thread-safe; callable from HTTP handler threads.

        ``trusted`` is True only for in-process channels (terminal, slash
        command). Remote callers must present the decision token *and* the
        fingerprint of the operation they believe they are approving.
        """
        try:
            decision = ApprovalDecision(decision)
            scope = ApprovalScope(scope)
        except ValueError as exc:
            self._reject(approval_id, via, by, "invalid_decision")
            raise ApprovalError("invalid_decision", f"invalid decision/scope: {exc}", 400) from exc

        with self._lock:
            pending = self._pending.get(approval_id)
            if pending is None:
                row = self.store.get_approval(approval_id)
                if row is None:
                    self._reject(approval_id, via, by, "unknown")
                    raise ApprovalError("unknown", "unknown approval id", 404)
                self._reject(approval_id, via, by, "already_decided")
                raise ApprovalError("already_decided", f"approval already {row['status']}", 409)
            request = pending.request
            if request.kind == "question":
                self._reject(approval_id, via, by, "not_an_approval")
                raise ApprovalError("not_an_approval", "this id is a question; use answer", 400)
            if request.is_expired(self.clock()):
                self._resolve_locked(pending, ApprovalStatus.EXPIRED, reason="expired")
                self._reject(approval_id, via, by, "expired")
                raise ApprovalError("expired", "approval expired", 410)
            if not trusted:
                if not request.remote_allowed:
                    self._reject(approval_id, via, by, "remote_not_allowed")
                    raise ApprovalError(
                        "remote_not_allowed", "this operation must be approved locally", 403
                    )
                if not decision_token or not hmac.compare_digest(
                    decision_token, request.decision_token
                ):
                    self._reject(approval_id, via, by, "invalid_token")
                    raise ApprovalError("invalid_token", "invalid decision token", 403)
                if fingerprint is None or not hmac.compare_digest(fingerprint, request.fingerprint):
                    self._reject(approval_id, via, by, "operation_mismatch")
                    raise ApprovalError(
                        "operation_mismatch",
                        "the operation presented differs from the pending operation",
                        409,
                    )
            if scope == ApprovalScope.PROJECT and not trusted:
                self._reject(approval_id, via, by, "scope_not_allowed")
                raise ApprovalError(
                    "scope_not_allowed", "project scope can only be granted at the terminal", 403
                )
            if (
                decision == ApprovalDecision.APPROVE
                and scope in {ApprovalScope.SESSION, ApprovalScope.PROJECT}
                and not request.session_scope_allowed
            ):
                self._reject(approval_id, via, by, "scope_not_allowed")
                raise ApprovalError(
                    "scope_not_allowed", "session scope is not permitted for this operation", 403
                )
            status = (
                ApprovalStatus.APPROVED
                if decision == ApprovalDecision.APPROVE
                else ApprovalStatus.DENIED
            )
            self.events.emit(
                EventType.APPROVAL_RECEIVED,
                session_id=self.session_id,
                approval_id=approval_id,
                channel=via,
                client=by,
                decision=decision.value,
                scope=scope.value,
            )
            result = self._resolve_locked(
                pending, status, decision=decision, scope=scope, via=via, by=by
            )
        self._notify_pending_changed()
        return result

    def cancel(self, approval_id: str, reason: str = "canceled") -> DecisionResult | None:
        with self._lock:
            pending = self._pending.get(approval_id)
            if pending is None:
                return None
            result = self._resolve_locked(pending, ApprovalStatus.CANCELED, reason=reason)
        self._notify_pending_changed()
        return result

    def cancel_all(self, reason: str = "canceled") -> int:
        with self._lock:
            ids = list(self._pending)
        for approval_id in ids:
            self.cancel(approval_id, reason)
        return len(ids)

    def sweep_expired(self) -> list[str]:
        now = self.clock()
        expired: list[str] = []
        with self._lock:
            for approval_id, pending in list(self._pending.items()):
                if pending.request.is_expired(now):
                    self._resolve_locked(pending, ApprovalStatus.EXPIRED, reason="expired")
                    expired.append(approval_id)
        if expired:
            self._notify_pending_changed()
        return expired

    def _expire(self, approval_id: str) -> DecisionResult:
        with self._lock:
            pending = self._pending.get(approval_id)
            if pending is None:
                # Decided in the tiny window between timeout and this call.
                row = self.store.get_approval(approval_id)
                return (
                    self._result_from_row(row)
                    if row
                    else DecisionResult(
                        approval_id=approval_id, status=ApprovalStatus.EXPIRED, reason="expired"
                    )
                )
            if pending.future.done():
                return pending.future.result()
            return self._resolve_locked(pending, ApprovalStatus.EXPIRED, reason="expired")

    # -- restart safety ---------------------------------------------------------------
    def recover(self) -> list[dict[str, Any]]:
        """Cancel approvals left pending by a previous process. Never re-arms them."""
        stale = self.store.pending_approvals_from_other_processes(self.process_id)
        now = self.clock().isoformat()
        for row in stale:
            self.store.update_approval(
                row["id"],
                status=ApprovalStatus.CANCELED.value,
                decided_at=now,
                resolution_reason="process_restart",
            )
            self.events.emit(
                EventType.APPROVAL_CANCELED,
                session_id=row["session_id"],
                approval_id=row["id"],
                tool=row["tool"],
                summary=row["summary"],
                reason="process_restart",
            )
        return stale

    # -- internals ----------------------------------------------------------------------
    def _resolve_locked(
        self,
        pending: _Pending,
        status: ApprovalStatus,
        *,
        decision: ApprovalDecision | None = None,
        scope: ApprovalScope | None = None,
        via: str | None = None,
        by: str | None = None,
        reason: str | None = None,
    ) -> DecisionResult:
        request = pending.request
        now = self.clock()
        request.status = status
        result = DecisionResult(
            approval_id=request.approval_id,
            status=status,
            decision=decision,
            scope=scope,
            decided_at=now,
            via=via,
            by=by,
            reason=reason,
        )
        self.store.update_approval(
            request.approval_id,
            status=status.value,
            decision=decision.value if decision else None,
            decided_scope=scope.value if scope else None,
            decided_at=now.isoformat(),
            decided_via=via,
            decided_by=by,
            resolution_reason=reason,
        )
        event_type = {
            ApprovalStatus.APPROVED: EventType.APPROVAL_APPROVED,
            ApprovalStatus.DENIED: EventType.APPROVAL_DENIED,
            ApprovalStatus.EXPIRED: EventType.APPROVAL_EXPIRED,
            ApprovalStatus.CANCELED: EventType.APPROVAL_CANCELED,
            ApprovalStatus.SUPERSEDED: EventType.APPROVAL_SUPERSEDED,
        }[status]
        self.events.emit(
            event_type,
            session_id=self.session_id,
            **self._event_fields(request),
            channel=via,
            client=by,
            scope=scope.value if scope else None,
            reason=reason,
        )
        self._pending.pop(request.approval_id, None)
        if not pending.future.done():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is pending.loop:
                pending.future.set_result(result)
            else:
                pending.loop.call_soon_threadsafe(_set_result_if_pending, pending.future, result)
        for channel in list(self._channels):
            _fire_and_forget(pending.loop, channel.withdraw(request, result))
        return result

    def _reject(self, approval_id: str, via: str, by: str | None, code: str) -> None:
        self.events.emit(
            EventType.APPROVAL_REJECTED,
            session_id=self.session_id,
            approval_id=approval_id,
            channel=via,
            client=by,
            code=code,
        )

    @staticmethod
    def _event_fields(request: ApprovalRequest) -> dict[str, Any]:
        return {
            "approval_id": request.approval_id,
            "task_id": request.task_id,
            "tool": request.tool,
            "category": request.category.value,
            "risk": request.risk.value,
            "summary": request.summary,
            "command": request.command,
            "cwd": request.cwd,
            "affected_files": list(request.affected_files),
            "machine": request.machine,
            "fingerprint": request.fingerprint,
            "expires_at": request.expires_at.isoformat(),
            "remote_allowed": request.remote_allowed,
        }

    @staticmethod
    def _result_from_row(row: dict[str, Any]) -> DecisionResult:
        return DecisionResult(
            approval_id=row["id"],
            status=ApprovalStatus(row["status"]),
            decision=ApprovalDecision(row["decision"]) if row.get("decision") else None,
            scope=ApprovalScope(row["decided_scope"]) if row.get("decided_scope") else None,
            decided_at=datetime.fromisoformat(row["decided_at"]) if row.get("decided_at") else None,
            via=row.get("decided_via"),
            by=row.get("decided_by"),
            reason=row.get("resolution_reason"),
        )


def _set_result_if_pending(future: asyncio.Future, result: DecisionResult) -> None:
    if not future.done():
        future.set_result(result)


def _fire_and_forget(loop: asyncio.AbstractEventLoop, coro) -> None:
    def _schedule() -> None:
        task = loop.create_task(coro)
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        _schedule()
    else:
        loop.call_soon_threadsafe(_schedule)
