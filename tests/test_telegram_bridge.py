"""Telegram remote control: prompts, steering, answers, commands, auth, shared poller."""

import asyncio
from pathlib import Path

import httpx
from rich.console import Console

from trendlab.app import TrendLabApp
from trendlab.config.loader import load_config
from trendlab.config.schema import (
    NotificationsConfig,
    PermissionMode,
    RemoteApprovalConfig,
    TelegramBridgeConfig,
    TelegramNotificationConfig,
)
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.remote.telegram_bridge import TelegramBridge, _plain
from trendlab.telemetry.events import EventType
from trendlab.ui.commands import CommandRouter

from .test_batch_c import FakeTelegram, _button


def _tl(project: Path, provider, mode=PermissionMode.UNSAFE):
    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    cfg.remote_approval = RemoteApprovalConfig(enabled=False, port=0)
    cfg.notifications = NotificationsConfig(telegram=TelegramNotificationConfig(chat_id="-55"))
    return TrendLabApp(
        project,
        cfg,
        provider=provider,
        model_ref="scripted:m",
        permission_mode=mode,
        console=Console(record=True, width=100, force_terminal=False),
    )


async def _bridge(tl: TrendLabApp, fake: FakeTelegram) -> TelegramBridge:
    br = TelegramBridge(
        tl,
        TelegramBridgeConfig(enabled=True),
        tl.config.notifications.telegram,
        tl.events,
        client=httpx.AsyncClient(transport=fake.transport()),
        poll_timeout=0,
        poll_interval=0.01,
    )
    await br.start()
    tl.telegram_bridge = br
    return br


def _msg(uid: int, text: str, chat="-55", reply_to=None) -> dict:
    m = {"message_id": uid + 1000, "from": {"id": 7}, "chat": {"id": chat}, "text": text}
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": uid, "message": m}


async def _wait(cond, n=300):
    for _ in range(n):
        await asyncio.sleep(0.01)
        if cond():
            return True
    return False


async def test_text_runs_prompt_and_reports_back(project: Path, _trendlab_home: Path, monkeypatch):
    monkeypatch.setenv("TRENDLAB_TELEGRAM_BOT_TOKEN", "1:x")
    fake = FakeTelegram()
    tl = _tl(project, ScriptedProvider([ModelResponse(text="All good — nothing to change.")]))
    await tl.start(interactive=False)
    try:
        br = await _bridge(tl, fake)
        assert await _wait(lambda: fake.sent("sendMessage"))
        assert "TrendLab online" in fake.sent("sendMessage")[0]["text"]
        fake.updates.append(_msg(1, "what does this repo do?"))
        assert await _wait(lambda: len(fake.sent("sendMessage")) >= 3)
        texts = [m["text"] for m in fake.sent("sendMessage")]
        assert any(t.startswith("▶ Started: what does this repo do?") for t in texts)
        done = next(t for t in texts if "✅ Done" in t and "Cost:" in t)
        assert done.startswith(
            "All good — nothing to change."
        )  # the answer itself, then the footer
        assert br.status()["messages_in"] == 1 and br.status()["running"]
        assert tl.store.events(tl.session_id)  # audit trail includes the remote message
        types = [e["type"] for e in tl.store.events(tl.session_id)]
        assert EventType.REMOTE_MESSAGE.value in types
        await br.stop()
        assert "session ended" in fake.sent("sendMessage")[-1]["text"]
        assert not br.running
    finally:
        await tl.stop()


async def test_steering_answers_and_commands(project: Path, _trendlab_home: Path, monkeypatch):
    monkeypatch.setenv("TRENDLAB_TELEGRAM_BOT_TOKEN", "1:x")
    fake = FakeTelegram()
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="q",
                        name="ask_user",
                        arguments={"question": "Which DB?", "options": ["sqlite", "pg"]},
                    )
                ]
            ),
            ModelResponse(text="using sqlite; nothing changed"),
        ]
    )
    tl = _tl(project, provider)
    await tl.start(interactive=False)
    try:
        br = await _bridge(tl, fake)
        await _wait(lambda: fake.sent("sendMessage"))
        fake.updates.append(_msg(1, "set up the database"))
        assert await _wait(
            lambda: any(m["text"].startswith("❓ Which DB?") for m in fake.sent("sendMessage"))
        )
        assert br._pending_question is not None
        # Text while the agent is waiting on a question answers it (not steering).
        fake.updates.append(_msg(2, "sqlite"))
        assert await _wait(
            lambda: any(
                m["text"].startswith("💬 Answered: sqlite") for m in fake.sent("sendMessage")
            )
        )
        assert await _wait(lambda: any("✅ Done" in m["text"] for m in fake.sent("sendMessage")))
        assert br._pending_question is None
        # Commands run through the router; output comes back; dangerous ones are refused.
        n = len(fake.sent("sendMessage"))
        fake.updates.append(_msg(3, "/status"))
        assert await _wait(lambda: len(fake.sent("sendMessage")) > n)
        assert "scripted:m" in fake.sent("sendMessage")[-1]["text"]
        for i, cmd in enumerate(["/quit", "/mode ask", "/approvals approve abc"], start=4):
            n = len(fake.sent("sendMessage"))
            fake.updates.append(_msg(i, cmd))
            assert await _wait(lambda n=n: len(fake.sent("sendMessage")) > n)
            assert "terminal-only" in fake.sent("sendMessage")[-1]["text"]
        assert tl.engine.mode == PermissionMode.UNSAFE
        # Wrong chat: ignored and audited.
        n = len(fake.sent("sendMessage"))
        fake.updates.append(_msg(9, "hack", chat="999"))
        await asyncio.sleep(0.1)
        assert len(fake.sent("sendMessage")) == n
        assert any(
            e["type"] == EventType.REMOTE_AUTH_FAILED.value for e in tl.store.events(tl.session_id)
        )
        # /stop with nothing running
        fake.updates.append(_msg(10, "/stop"))
        assert await _wait(lambda: fake.sent("sendMessage")[-1]["text"] == "Nothing is running.")
        # /telegram status at the terminal
        console = Console(record=True, width=120, force_terminal=False)
        await CommandRouter(tl, console).dispatch("/telegram")
        assert "remote control ON" in console.export_text()
    finally:
        await tl.stop()


async def test_steering_while_running(project: Path, _trendlab_home: Path, monkeypatch):
    monkeypatch.setenv("TRENDLAB_TELEGRAM_BOT_TOKEN", "1:x")
    fake = FakeTelegram()

    class SlowProvider(ScriptedProvider):
        async def complete(self, *a, **kw):
            await asyncio.sleep(0.3)
            return await super().complete(*a, **kw)

    tl = _tl(project, SlowProvider([ModelResponse(text="done; nothing changed")]))
    await tl.start(interactive=False)
    try:
        await _bridge(tl, fake)
        await _wait(lambda: fake.sent("sendMessage"))
        fake.updates.append(_msg(1, "do the thing"))
        assert await _wait(lambda: tl.run_in_progress())
        fake.updates.append(_msg(2, "also add tests"))
        assert await _wait(
            lambda: any(m["text"].startswith("↳ Steering queued") for m in fake.sent("sendMessage"))
        )
        assert "also add tests" in tl.agent._steer or any(
            m.get("content") == "also add tests" for m in tl.context.messages
        )
        assert await _wait(
            lambda: any("✅ Done" in m["text"] for m in fake.sent("sendMessage")), n=600
        )
    finally:
        await tl.stop()


async def test_bridge_shares_poller_with_button_channel(
    project: Path, _trendlab_home: Path, monkeypatch
):
    monkeypatch.setenv("TRENDLAB_TELEGRAM_BOT_TOKEN", "1:x")
    fake = FakeTelegram()
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(id="w", name="write_file", arguments={"path": "n.txt", "content": "x"})
                ]
            ),
            ModelResponse(text="written; validation not possible here"),
        ]
    )
    tl = _tl(project, provider, mode=PermissionMode.ASK)
    tl.config.remote_approval = RemoteApprovalConfig(enabled=False, port=0, telegram=True)
    await tl.start(interactive=False)
    try:
        br = await _bridge(tl, fake)
        await _wait(lambda: fake.sent("sendMessage"))
        # Turning remote approvals on attaches the button channel to the bridge's poller.
        monkeypatch.setattr(tl, "enable_remote", _enable_remote_without_web(tl, fake))
        await tl.enable_remote(persist=False)
        assert tl.telegram_channel is not None and tl.telegram_channel.poller is br.poller
        assert tl.telegram_channel.status()["polling"] and tl.telegram_channel._task is None
        fake.updates.append(_msg(1, "write n.txt"))
        assert await _wait(
            lambda: any(
                "Approve once" in str(m.get("reply_markup")) for m in fake.sent("sendMessage")
            )
        )
        msg = next(
            m for m in fake.sent("sendMessage") if "Approve once" in str(m.get("reply_markup"))
        )
        fake.updates.append(
            {
                "update_id": 2,
                "callback_query": {
                    "id": "c1",
                    "from": {"id": 7},
                    "message": {"message_id": 101, "chat": {"id": "-55"}},
                    "data": _button(msg, "✅ Approve once"),
                },
            }
        )
        assert await _wait(lambda: (project / "n.txt").exists(), n=600)
        assert await _wait(lambda: any("✅ Done" in m["text"] for m in fake.sent("sendMessage")))
        # No duplicate "approval needed" text while the button channel is on.
        assert not any(m["text"].startswith("⏳ Approval needed") for m in fake.sent("sendMessage"))
    finally:
        await tl.stop()


def _enable_remote_without_web(tl, fake):
    """enable_remote without binding the web server (ports are not needed for this test)."""
    from trendlab.approvals.channels.telegram import TelegramChannel

    async def _enable(persist: bool = True):
        tg = TelegramChannel(
            tl.config.notifications.telegram,
            tl.events,
            client=httpx.AsyncClient(transport=fake.transport()),
            poll_timeout=0,
            poll_interval=0.01,
        )
        if tl.telegram_bridge is not None and tl.telegram_bridge.poller is not None:
            tg.poller = tl.telegram_bridge.poller
        tl.config.remote_approval.enabled = True
        await tl.approvals.add_channel(tg)
        tl.telegram_channel = tg
        return "n/a"

    return _enable


def test_plain_strips_markup():
    assert _plain("## Head\n[bold green]Done[/] **ok** `x`") == "Head\nDone ok x"


async def test_bridge_requires_token_and_chat(project: Path, _trendlab_home: Path, monkeypatch):
    import pytest

    from trendlab.remote.telegram_bridge import TelegramError

    monkeypatch.delenv("TRENDLAB_TELEGRAM_BOT_TOKEN", raising=False)
    tl = _tl(project, ScriptedProvider([ModelResponse(text="x")]))
    await tl.start(interactive=False)
    try:
        with pytest.raises(TelegramError, match="bot token"):
            await tl.enable_telegram_bridge()
        monkeypatch.setenv("TRENDLAB_TELEGRAM_BOT_TOKEN", "1:x")
        tl.config.notifications.telegram.chat_id = None
        with pytest.raises(TelegramError, match="chat_id"):
            await tl.enable_telegram_bridge()
        console = Console(record=True, width=120, force_terminal=False)
        await CommandRouter(tl, console).dispatch("/telegram")
        assert "off" in console.export_text()
    finally:
        await tl.stop()


async def test_model_from_telegram_lists_and_never_blocks(
    project: Path, _trendlab_home: Path, monkeypatch
):
    monkeypatch.setenv("TRENDLAB_TELEGRAM_BOT_TOKEN", "1:x")
    fake = FakeTelegram()
    tl = _tl(project, ScriptedProvider([ModelResponse(text="ok")]))
    await tl.start(interactive=False)
    try:
        await _bridge(tl, fake)
        await _wait(lambda: fake.sent("sendMessage"))
        n = len(fake.sent("sendMessage"))
        fake.updates.append(_msg(1, "/model"))
        assert await _wait(lambda: len(fake.sent("sendMessage")) > n)
        reply = fake.sent("sendMessage")[-1]["text"]
        assert "scripted:m" in reply and "Switch with: /model <name>" in reply
        # The next message still gets through immediately (nothing is blocked on stdin).
        n = len(fake.sent("sendMessage"))
        fake.updates.append(_msg(2, "/status"))
        assert await _wait(lambda: len(fake.sent("sendMessage")) > n)
        assert "scripted:m" in fake.sent("sendMessage")[-1]["text"]
    finally:
        await tl.stop()
