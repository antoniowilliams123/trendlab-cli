"""Telegram remote control (spec §90.12): talk to the running terminal session from your phone.

Messages sent to the configured chat become prompts (when idle), steering (while a run is in
progress) or answers (when the agent asked a question). Slash commands run through the same
``CommandRouter`` as the terminal and their output comes back as a message. The session reports
start, completion, failure and questions back to the chat.

One ``TelegramPoller`` owns the bot's ``getUpdates`` stream for the whole process; the
inline-button approval channel (``TelegramChannel``) subscribes to it when both are on, because
Telegram allows a single poller per bot token.

Security: only the configured ``chat_id`` is honoured (everything else is ignored and audited);
commands that change permissions, decide approvals or end the session are refused remotely; the
bot token comes from the secrets store and is never logged.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from rich.console import Console

from trendlab.config.schema import TelegramBridgeConfig, TelegramNotificationConfig
from trendlab.security.redaction import redact_text
from trendlab.security.secrets import resolve_secret
from trendlab.telemetry.events import Event, EventBus, EventType

UpdateHandler = Callable[[dict[str, Any]], Awaitable[None]]

MAX_CHUNK = 3800
# Decided at the keyboard only: permission changes, approval decisions, session end, local I/O.
DENIED_COMMANDS = {
    "/quit",
    "/exit",
    "/clear",
    "/edit",
    "/paste",
    "/image",
    "/mode",
    "/remote",
    "/permissions",
    "/telegram",
    "/mouse",
}
DENIED_PREFIXES = ("/approvals approve", "/approvals deny", "/cost-limit", "/worktree")


class TelegramError(Exception):
    pass


class TelegramPoller:
    """Single ``getUpdates`` loop per bot token, fanning updates out to subscribers."""

    def __init__(
        self,
        cfg: TelegramNotificationConfig,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        poll_timeout: int = 20,
        poll_interval: float = 1.0,
    ) -> None:
        self.cfg = cfg
        self._token = token
        self._client = client or httpx.AsyncClient(timeout=poll_timeout + 10)
        self._own_client = client is None
        self._poll_timeout = poll_timeout
        self._poll_interval = poll_interval
        self._handlers: list[UpdateHandler] = []
        self._task: asyncio.Task | None = None
        self._offset: int | None = None
        self.last_error: str | None = None
        self.updates_seen = 0

    @property
    def api(self) -> str:
        return f"{self.cfg.api_base}/bot{self._token}"

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def subscribe(self, handler: UpdateHandler) -> None:
        if handler not in self._handlers:
            self._handlers.append(handler)

    def unsubscribe(self, handler: UpdateHandler) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    async def call(self, method: str, **payload: Any) -> Any:
        try:
            resp = await self._client.post(f"{self.api}/{method}", json=payload)
        except httpx.HTTPError as exc:
            raise TelegramError(f"telegram: network error: {exc.__class__.__name__}") from exc
        if resp.status_code >= 400:
            raise TelegramError(f"telegram: HTTP {resp.status_code} on {method}")
        body = resp.json()
        if not body.get("ok", False):
            raise TelegramError(f"telegram: {method} failed: {body.get('description', '?')}")
        return body.get("result")

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._loop(), name="telegram-poller")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._own_client:
            await self._client.aclose()

    async def _loop(self) -> None:
        while True:
            try:
                updates = await self.call(
                    "getUpdates",
                    timeout=self._poll_timeout,
                    offset=self._offset,
                    allowed_updates=["message", "callback_query"],
                )
                for upd in updates or []:
                    self._offset = int(upd["update_id"]) + 1
                    self.updates_seen += 1
                    for handler in list(self._handlers):
                        try:
                            await handler(upd)
                        except Exception as exc:  # noqa: BLE001 — one bad handler must not stop polling
                            self.last_error = f"handler: {exc.__class__.__name__}: {exc}"[:200]
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — survive network blips / 409 conflicts
                self.last_error = f"{exc.__class__.__name__}: {exc}"[:200]
                await asyncio.sleep(max(self._poll_interval, 2.0))
                continue
            if self._poll_interval:
                await asyncio.sleep(self._poll_interval)


def _plain(text: str) -> str:
    """Strip Rich markup and Markdown decorations so the message reads well in Telegram."""
    text = re.sub(r"\[/\]|\[/?[a-zA-Z#][^\]]*\]", "", text)  # rich tags, incl. [/]
    text = re.sub(r"^\s*#{1,6}\s+", "", text, flags=re.M)  # markdown headings
    text = text.replace("**", "").replace("`", "")
    return text.strip()


class TelegramBridge:
    name = "telegram-bridge"

    def __init__(
        self,
        app: Any,
        cfg: TelegramBridgeConfig,
        tg_cfg: TelegramNotificationConfig,
        events: EventBus,
        *,
        client: httpx.AsyncClient | None = None,
        poll_timeout: int = 20,
        poll_interval: float = 1.0,
    ) -> None:
        self.app = app
        self.cfg = cfg
        self.tg_cfg = tg_cfg
        self.events = events
        self._client = client
        self._poll_timeout = poll_timeout
        self._poll_interval = poll_interval
        self.poller: TelegramPoller | None = None
        self.started_at: float | None = None
        self.messages_in = 0
        self.messages_out = 0
        self.last_error: str | None = None
        self._pending_question: str | None = None  # approval_id of an unanswered question
        self._router: Any = None
        self._unsubscribe: Callable[[], None] | None = None

    # -- lifecycle ---------------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self.poller is not None and self.poller.running

    async def start(self) -> None:
        token = resolve_secret(self.tg_cfg.bot_token_env)
        if not token:
            raise TelegramError(
                f"no bot token ({self.tg_cfg.bot_token_env}); "
                f"run: trendlab secret set {self.tg_cfg.bot_token_env}"
            )
        if not self.tg_cfg.chat_id:
            raise TelegramError("notifications.telegram.chat_id is not configured")
        self.poller = TelegramPoller(
            self.tg_cfg,
            token,
            client=self._client,
            poll_timeout=self._poll_timeout,
            poll_interval=self._poll_interval,
        )
        self.poller.subscribe(self.handle_update)
        channel = getattr(self.app, "telegram_channel", None)
        if channel is not None:
            await channel.use_poller(self.poller)  # one getUpdates stream per bot token
        self.poller.start()
        self._unsubscribe = self.events.subscribe(self._on_event)
        from trendlab.ui.commands import CommandRouter

        self._router = CommandRouter(
            self.app,
            Console(record=True, width=72, force_terminal=False, no_color=True),
            prompt_cb=self._run_prompt,
        )
        self.started_at = time.monotonic()
        self.events.emit(
            EventType.REMOTE_CHANNEL_STARTED,
            session_id=self.app.session_id,
            channel=self.name,
            chat_id=str(self.tg_cfg.chat_id),
        )
        await self.send(
            f"🟢 TrendLab online · {self.app.project_root.name} · {self.app.model_ref}\n"
            "Send a task to start it, text while it runs to steer it, /status /plan /diff /cost "
            "/stop for control, /help for everything."
        )

    async def stop(self, *, announce: bool = True) -> None:
        if self.poller is None:
            return
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if announce:
            try:
                await self.send("⚪ TrendLab session ended.")
            except TelegramError:
                pass
        channel = getattr(self.app, "telegram_channel", None)
        if channel is not None and getattr(channel, "poller", None) is self.poller:
            channel.poller = None
        await self.poller.stop()
        self.poller = None
        self.events.emit(
            EventType.REMOTE_CHANNEL_STOPPED, session_id=self.app.session_id, channel=self.name
        )

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "chat_id": self.tg_cfg.chat_id,
            "messages_in": self.messages_in,
            "messages_out": self.messages_out,
            "updates_seen": self.poller.updates_seen if self.poller else 0,
            "last_error": self.last_error or (self.poller.last_error if self.poller else None),
            "uptime_s": round(time.monotonic() - self.started_at, 1) if self.started_at else 0,
        }

    # -- outbound ----------------------------------------------------------------------------------
    async def send(self, text: str) -> None:
        if self.poller is None:
            return
        text = redact_text(_plain(text)) or "(empty)"
        chunks = [text[i : i + MAX_CHUNK] for i in range(0, len(text), MAX_CHUNK)] or [text]
        for i, chunk in enumerate(chunks):
            try:
                await self.poller.call(
                    "sendMessage",
                    chat_id=self.tg_cfg.chat_id,
                    text=chunk,
                    disable_web_page_preview=True,
                )
                self.messages_out += 1
            except TelegramError as exc:
                self.last_error = str(exc)
                return
            if i < len(chunks) - 1:
                await asyncio.sleep(0.5)

    def _on_event(self, event: Event) -> None:
        d = event.data
        if d.get("role", "main") != "main":
            return
        t = event.type
        text: str | None = None
        if t in {EventType.RUN_COMPLETED, EventType.RUN_FAILED, EventType.RUN_CANCELED}:
            head = {
                EventType.RUN_COMPLETED: "✅ Done",
                EventType.RUN_FAILED: "❌ Stopped",
                EventType.RUN_CANCELED: "■ Canceled",
            }[t]
            parts = [head]
            if d.get("stop_reason"):
                parts.append(f"Reason: {d['stop_reason']}")
            if d.get("changed_files"):
                files = d["changed_files"]
                parts.append(
                    "Changed: "
                    + ", ".join(files[:6])
                    + (f" (+{len(files) - 6})" if len(files) > 6 else "")
                )
            if "validated" in d:
                parts.append(f"Validated: {'yes' if d['validated'] else 'no'}")
            if d.get("cost_usd") is not None:
                parts.append(f"Cost: ${d['cost_usd']:.4f}")
            text = "\n".join(parts)
        elif t == EventType.QUESTION_ASKED:
            channel = getattr(self.app, "telegram_channel", None)
            if channel is None:  # with the button channel on, the question arrives with buttons
                self._pending_question = d.get("approval_id")
                opts = d.get("options") or []
                text = f"❓ {d.get('question')}"
                if opts:
                    text += "\nOptions: " + " / ".join(opts)
                text += "\nReply here to answer."
        elif t in {
            EventType.QUESTION_ANSWERED,
            EventType.APPROVAL_CANCELED,
            EventType.APPROVAL_EXPIRED,
        }:
            if d.get("approval_id") == self._pending_question:
                self._pending_question = None
        elif t == EventType.APPROVAL_REQUESTED:
            if getattr(self.app, "telegram_channel", None) is None:
                text = (
                    f"⏳ Approval needed: {d.get('summary')}\n"
                    "Approve at the terminal (y) — or turn on remote_approval.telegram "
                    "for buttons here."
                )
        elif t == EventType.LOOP_DETECTED and d.get("action") == "stop":
            text = f"⚠️ No progress: {d.get('reason')}"
        if text:
            self._fire(self.send(text))

    def _fire(self, coro: Awaitable[None]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(coro)  # type: ignore[arg-type]

    # -- inbound -----------------------------------------------------------------------------------
    def _authorized(self, chat_id: Any) -> bool:
        return str(chat_id) == str(self.tg_cfg.chat_id)

    async def handle_update(self, upd: dict[str, Any]) -> None:
        msg = upd.get("message")
        if not msg:
            return  # callback queries belong to the button channel
        chat_id = (msg.get("chat") or {}).get("id")
        who = f"tg:{(msg.get('from') or {}).get('id', '?')}"
        text = str(msg.get("text") or "").strip()
        if not text:
            return
        if not self._authorized(chat_id):
            self.events.emit(
                EventType.REMOTE_AUTH_FAILED,
                session_id=self.app.session_id,
                channel=self.name,
                client=who,
                chat_id=str(chat_id),
                reason="wrong_chat",
            )
            return
        self.messages_in += 1
        self.events.emit(
            EventType.REMOTE_MESSAGE,
            session_id=self.app.session_id,
            channel=self.name,
            client=who,
            text=text[:300],
        )
        await self.handle_text(text, who)

    async def handle_text(self, text: str, who: str = "tg") -> None:
        # 1) An unanswered question takes any text as the answer.
        if self._pending_question and not text.startswith("/"):
            try:
                self.app.approvals.answer(
                    self._pending_question, text, via=self.name, by=who, trusted=True
                )
                self._pending_question = None
                await self.send(f"💬 Answered: {text[:200]}")
            except Exception as exc:  # noqa: BLE001 — expired/decided elsewhere
                self._pending_question = None
                await self.send(
                    f"Could not answer that question ({exc}); treating as a new message."
                )
                await self.handle_text(text, who)
            return
        # 2) Slash commands.
        if text.startswith("/"):
            await self._command(text)
            return
        # 3) Steering while a run is active, otherwise a new prompt.
        agent = self.app.agent
        if agent is not None and self.app.run_in_progress():
            agent.steer(text)
            await self.send(
                f"↳ Steering queued (applied before the next model call):\n{text[:300]}"
            )
            return
        await self.send(f"▶ Started: {text[:300]}")
        await self._run_prompt(text)

    async def _run_prompt(self, text: str) -> None:
        self.app.remote_prompt(text)

    async def _command(self, text: str) -> None:
        if not self.cfg.allow_commands:
            await self.send(
                "Slash commands are disabled for Telegram (telegram_bridge.allow_commands)."
            )
            return
        lowered = text.lower()
        head = lowered.split()[0]
        if head in {"/stop", "/esc", "/interrupt"}:
            if self.app.run_in_progress():
                self.app.cancel_run()
                await self.send("■ Interrupt sent.")
            else:
                await self.send("Nothing is running.")
            return
        if head in DENIED_COMMANDS or lowered.startswith(DENIED_PREFIXES):
            await self.send(
                f"{head} is terminal-only (permissions, approvals and session control stay local)."
            )
            return
        if head == "/start":
            await self.send("Hi — send a task, or /help.")
            return
        console = self._router.console
        console.export_text(clear=True)
        try:
            await self._router.dispatch(text)
        except Exception as exc:  # noqa: BLE001
            await self.send(f"{head} failed: {exc.__class__.__name__}: {exc}")
            return
        out = console.export_text(clear=True).strip()
        await self.send(out or f"{head}: ok")
