"""Telegram inline-button approval channel (spec §90).

Each pending approval is posted to the configured chat as a message with buttons
(Approve once · Session · Deny); questions get one button per option plus "reply to answer".
Decisions arrive through ``getUpdates`` long polling — no public URL, no webhook, works from a
laptop behind NAT.

Security model (same rules as the web channel, enforced by the ApprovalManager):

* only the configured ``chat_id`` may decide; anything else is ignored and audited;
* ``callback_data`` carries an HMAC of ``approval_id:action`` keyed by the request's per-request
  decision token, so a forged or replayed button cannot decide a different request;
* the manager still checks expiry, single-use, remote-allowed (high-risk stays local) and the
  operation fingerprint;
* bot token comes from the secrets store / env and is never logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from typing import Any

import httpx

from trendlab.approvals.channels.base import ApprovalChannel, ChannelError
from trendlab.approvals.models import (
    ApprovalDecision,
    ApprovalError,
    ApprovalRequest,
    ApprovalScope,
    ApprovalStatus,
    DecisionResult,
)
from trendlab.config.schema import TelegramNotificationConfig
from trendlab.security.redaction import redact_text
from trendlab.security.secrets import resolve_secret
from trendlab.telemetry.events import EventBus, EventType

_ACTIONS = {
    "once": (ApprovalDecision.APPROVE, ApprovalScope.ONCE),
    "sess": (ApprovalDecision.APPROVE, ApprovalScope.SESSION),
    "deny": (ApprovalDecision.DENY, ApprovalScope.ONCE),
}


def _mac(decision_token: str, approval_id: str, action: str) -> str:
    return hmac.new(
        decision_token.encode(), f"{approval_id}:{action}".encode(), hashlib.sha256
    ).hexdigest()[:16]


def callback_data(request: ApprovalRequest, action: str) -> str:
    mac = _mac(request.decision_token, request.approval_id, action)
    return f"tl|{request.approval_id}|{action}|{mac}"


def parse_callback(data: str) -> tuple[str, str, str] | None:
    parts = data.split("|")
    if len(parts) != 4 or parts[0] != "tl":
        return None
    return parts[1], parts[2], parts[3]


class TelegramChannel(ApprovalChannel):
    name = "telegram"
    remote = True

    def __init__(
        self,
        cfg: TelegramNotificationConfig,
        events: EventBus,
        *,
        project_name: str = "",
        client: httpx.AsyncClient | None = None,
        poll_timeout: int = 20,
        poll_interval: float = 1.0,
    ) -> None:
        self.cfg = cfg
        self.events = events
        self.project_name = project_name
        self._client = client or httpx.AsyncClient(timeout=poll_timeout + 10)
        self._poll_timeout = poll_timeout
        self._poll_interval = poll_interval
        self.manager: Any = None
        self._task: asyncio.Task | None = None
        self._offset: int | None = None
        self._messages: dict[str, int] = {}  # approval_id -> message_id
        self._by_message: dict[int, str] = {}  # message_id -> approval_id (for text replies)
        self._token: str | None = None
        self.last_error: str | None = None

    # -- lifecycle ---------------------------------------------------------------------------------
    @property
    def _api(self) -> str:
        return f"{self.cfg.api_base}/bot{self._token}"

    async def start(self, manager) -> None:
        self._token = resolve_secret(self.cfg.bot_token_env)
        if not self._token:
            raise ChannelError(
                f"telegram: no bot token ({self.cfg.bot_token_env}); trendlab secret set "
                f"{self.cfg.bot_token_env}"
            )
        if not self.cfg.chat_id:
            raise ChannelError("telegram: notifications.telegram.chat_id is not configured")
        self.manager = manager
        self._task = asyncio.create_task(self._poll_loop(), name="telegram-approvals")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await self._client.aclose()

    async def _call(self, method: str, **payload: Any) -> Any:
        try:
            resp = await self._client.post(f"{self._api}/{method}", json=payload)
        except httpx.HTTPError as exc:
            raise ChannelError(f"telegram: network error: {exc.__class__.__name__}") from exc
        if resp.status_code >= 400:
            raise ChannelError(f"telegram: HTTP {resp.status_code} on {method}")
        body = resp.json()
        if not body.get("ok", False):
            raise ChannelError(f"telegram: {method} failed: {body.get('description', '?')}")
        return body.get("result")

    # -- presenting --------------------------------------------------------------------------------
    def _text(self, r: ApprovalRequest) -> str:
        if r.kind == "question":
            lines = [f"❓ TrendLab asks ({self.project_name or r.machine}):", r.explanation]
            if r.options:
                lines.append("Tap an option or reply to this message with your answer.")
            else:
                lines.append("Reply to this message with your answer.")
        else:
            lines = [
                f"⏳ Approval · risk {r.risk.value.upper()} · {self.project_name or r.machine}",
                f"Action: {r.summary}",
            ]
            if r.command:
                lines.append(f"Command: {r.command[:300]}")
            if r.affected_files:
                shown = ", ".join(r.affected_files[:5])
                if len(r.affected_files) > 5:
                    shown += f" (+{len(r.affected_files) - 5} more)"
                lines.append(f"Files: {shown}")
            if r.explanation:
                lines.append(f"Agent says: {r.explanation[:300]}")
            if r.preview:
                added = sum(
                    1
                    for ln in r.preview.splitlines()
                    if ln.startswith("+") and not ln.startswith("+++")
                )
                removed = sum(
                    1
                    for ln in r.preview.splitlines()
                    if ln.startswith("-") and not ln.startswith("---")
                )
                if added or removed:
                    lines.append(f"Diff: +{added} / -{removed} lines")
                elif r.tool == "plan":
                    lines.append("Plan:\n" + r.preview[:1500])
        lines.append(
            f"Expires in {max(1, r.seconds_remaining() // 60)} min · id {r.approval_id[:8]}"
        )
        return redact_text("\n".join(lines))[:3900]

    def _keyboard(self, r: ApprovalRequest) -> list[list[dict[str, str]]]:
        if r.kind == "question":
            return [
                [{"text": o[:40], "callback_data": callback_data(r, f"opt{i}")}]
                for i, o in enumerate(r.options[:8])
            ]
        row = [{"text": "✅ Approve once", "callback_data": callback_data(r, "once")}]
        if r.session_scope_allowed:
            row.append({"text": "✅ Session", "callback_data": callback_data(r, "sess")})
        return [row, [{"text": "⛔ Deny", "callback_data": callback_data(r, "deny")}]]

    async def dispatch(self, request: ApprovalRequest) -> None:
        if self.manager is None:
            raise ChannelError("telegram channel not started")
        result = await self._call(
            "sendMessage",
            chat_id=self.cfg.chat_id,
            text=self._text(request),
            reply_markup={"inline_keyboard": self._keyboard(request)},
        )
        mid = int((result or {}).get("message_id", 0))
        if mid:
            self._messages[request.approval_id] = mid
            self._by_message[mid] = request.approval_id

    async def withdraw(self, request: ApprovalRequest, result: DecisionResult) -> None:
        mid = self._messages.pop(request.approval_id, None)
        if mid is None:
            return
        self._by_message.pop(mid, None)
        label = {
            ApprovalStatus.APPROVED: "✅ Approved",
            ApprovalStatus.DENIED: "⛔ Denied",
            ApprovalStatus.EXPIRED: "⌛ Expired",
            ApprovalStatus.CANCELED: "✖ Canceled",
            ApprovalStatus.SUPERSEDED: "↻ Superseded",
        }.get(result.status, result.status.value)
        if request.kind == "question" and result.status == ApprovalStatus.APPROVED:
            label = "💬 Answered"
        via = f" via {result.via}" if result.via and result.via != self.name else ""
        try:
            await self._call(
                "editMessageText",
                chat_id=self.cfg.chat_id,
                message_id=mid,
                text=f"{label}{via} · {request.summary[:120]}",
            )
        except ChannelError as exc:
            self.last_error = str(exc)

    # -- receiving ---------------------------------------------------------------------------------
    async def _poll_loop(self) -> None:
        while True:
            try:
                updates = await self._call(
                    "getUpdates",
                    timeout=self._poll_timeout,
                    offset=self._offset,
                    allowed_updates=["callback_query", "message"],
                )
                for upd in updates or []:
                    self._offset = int(upd["update_id"]) + 1
                    await self.handle_update(upd)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — polling must survive network blips
                self.last_error = f"{exc.__class__.__name__}: {exc}"[:200]
                await asyncio.sleep(max(self._poll_interval, 2.0))
                continue
            if self._poll_interval:
                await asyncio.sleep(self._poll_interval)

    def _authorized(self, chat_id: Any) -> bool:
        return str(chat_id) == str(self.cfg.chat_id)

    async def handle_update(self, upd: dict[str, Any]) -> None:
        """Process one Telegram update (public for tests)."""
        if "callback_query" in upd:
            await self._handle_callback(upd["callback_query"])
        elif "message" in upd:
            await self._handle_message(upd["message"])

    async def _answer_cb(self, cb_id: str, text: str, alert: bool = False) -> None:
        try:
            await self._call(
                "answerCallbackQuery", callback_query_id=cb_id, text=text[:200], show_alert=alert
            )
        except ChannelError as exc:
            self.last_error = str(exc)

    def _reject(self, approval_id: str, who: str, code: str) -> None:
        self.events.emit(
            EventType.APPROVAL_REJECTED,
            session_id=getattr(self.manager, "session_id", ""),
            approval_id=approval_id,
            channel=self.name,
            client=who,
            code=code,
        )

    async def _handle_callback(self, cb: dict[str, Any]) -> None:
        cb_id = str(cb.get("id", ""))
        who = f"tg:{(cb.get('from') or {}).get('id', '?')}"
        chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")
        parsed = parse_callback(str(cb.get("data", "")))
        if parsed is None:
            await self._answer_cb(cb_id, "Unknown button")
            return
        approval_id, action, mac = parsed
        if not self._authorized(chat_id):
            self._reject(approval_id, who, "wrong_chat")
            await self._answer_cb(cb_id, "Not authorized", alert=True)
            return
        request = self.manager.get(approval_id)
        if request is None:
            self._reject(approval_id, who, "unknown_or_decided")
            await self._answer_cb(cb_id, "This request is no longer pending")
            return
        if not hmac.compare_digest(mac, _mac(request.decision_token, approval_id, action)):
            self._reject(approval_id, who, "invalid_token")
            await self._answer_cb(cb_id, "Invalid button (token mismatch)", alert=True)
            return
        try:
            if request.kind == "question":
                if not action.startswith("opt") or not action[3:].isdigit():
                    raise ApprovalError("invalid_decision", "bad option", 400)
                idx = int(action[3:])
                if idx >= len(request.options):
                    raise ApprovalError("invalid_decision", "bad option", 400)
                result = self.manager.answer(
                    approval_id,
                    request.options[idx],
                    via=self.name,
                    by=who,
                    decision_token=request.decision_token,
                )
                await self._answer_cb(cb_id, f"Answered: {request.options[idx]}")
                return
            if action not in _ACTIONS:
                raise ApprovalError("invalid_decision", "bad action", 400)
            decision, scope = _ACTIONS[action]
            result = self.manager.decide(
                approval_id,
                decision,
                scope,
                via=self.name,
                by=who,
                decision_token=request.decision_token,
                fingerprint=request.fingerprint,
            )
            await self._answer_cb(cb_id, f"{result.status.value.title()} ({scope.value})")
        except ApprovalError as exc:
            await self._answer_cb(cb_id, f"Refused: {exc}", alert=True)

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        chat_id = (msg.get("chat") or {}).get("id")
        text = str(msg.get("text") or "").strip()
        reply = msg.get("reply_to_message") or {}
        who = f"tg:{(msg.get('from') or {}).get('id', '?')}"
        if not text or not reply:
            return
        approval_id = self._by_message.get(int(reply.get("message_id", 0)))
        if approval_id is None:
            return
        if not self._authorized(chat_id):
            self._reject(approval_id, who, "wrong_chat")
            return
        request = self.manager.get(approval_id)
        if request is None or request.kind != "question":
            return
        try:
            self.manager.answer(
                approval_id, text, via=self.name, by=who, decision_token=request.decision_token
            )
        except ApprovalError as exc:
            self.last_error = str(exc)

    def status(self) -> dict[str, Any]:
        return {
            "chat_id": self.cfg.chat_id,
            "polling": bool(self._task and not self._task.done()),
            "pending_messages": len(self._messages),
            "last_error": self.last_error,
        }
