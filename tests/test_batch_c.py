"""Session branching, plan-approval gate, Telegram inline-button approvals."""

import asyncio
import json
from pathlib import Path

import httpx
from rich.console import Console

from trendlab.app import TrendLabApp
from trendlab.approvals.channels.telegram import TelegramChannel, parse_callback
from trendlab.approvals.models import ApprovalStatus
from trendlab.config.loader import load_config
from trendlab.config.schema import (
    NotificationsConfig,
    PermissionMode,
    RemoteApprovalConfig,
    TelegramNotificationConfig,
)
from trendlab.permissions.engine import PermissionRequest
from trendlab.permissions.models import OperationCategory
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.sessions.store import SessionStore
from trendlab.telemetry.events import EventType
from trendlab.ui.commands import CommandRouter


def _tl(project: Path, provider, mode=PermissionMode.UNSAFE, **cfg_over):
    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    for k, v in cfg_over.items():
        setattr(cfg, k, v)
    return TrendLabApp(
        project,
        cfg,
        provider=provider,
        model_ref="scripted:m",
        permission_mode=mode,
        console=Console(record=True, width=120, force_terminal=False),
    )


# -- branching -------------------------------------------------------------------------------------
def test_store_fork_and_tree(tmp_path: Path):
    store = SessionStore(tmp_path / "s.db")
    root = store.create_session("/p", "m", "x:y")
    for i in range(4):
        store.append_message(root, {"role": "user", "content": f"m{i}"})
    store.set_state(root, "plan", {"tasks": [1]})
    child = store.fork_session(root, "m", None, upto=2, label="try B")
    grandchild = store.fork_session(child, "m", "x:z")
    row = store.get_session(child)
    assert row["parent_id"] == root and row["branch_point"] == 2 and row["label"] == "try B"
    assert row["model"] == "x:y" and store.get_session(grandchild)["model"] == "x:z"
    assert [m["content"] for m in store.messages(child)] == ["m0", "m1"]
    assert store.get_state(child, "plan") == {"tasks": [1]}
    assert len(store.messages(root)) == 4  # parent untouched
    assert [c["id"] for c in store.children(root)] == [child]
    tree = store.session_tree("/p")
    assert [(d, r["id"]) for d, r in tree] == [(0, root), (1, child), (2, grandchild)]
    store.close()
    # Re-opening an existing database migrates/keeps the branch columns.
    again = SessionStore(tmp_path / "s.db")
    assert again.get_session(child)["parent_id"] == root
    again.close()


async def test_branch_command_forks_and_switches(project: Path, _trendlab_home: Path):
    tl = _tl(project, ScriptedProvider([ModelResponse(text="one"), ModelResponse(text="two")]))
    await tl.start(interactive=False)
    try:
        await tl.run_prompt("first")
        parent = tl.session_id
        n_parent = len(tl.store.messages(parent))
        console = Console(record=True, width=140, force_terminal=False)
        router = CommandRouter(tl, console)
        await router.dispatch("/branch try the other idea --keep 1")
        out = console.export_text(clear=True)
        child = tl.session_id
        assert child != parent and "Branched" in out and "try the other idea" in out
        assert len(tl.context.messages) == n_parent - 1
        row = tl.store.get_session(child)
        assert row["parent_id"] == parent and row["label"] == "try the other idea"
        await tl.run_prompt("second")  # continues on the child only
        assert len(tl.store.messages(parent)) == n_parent
        assert len(tl.store.messages(child)) > n_parent - 1
        await router.dispatch("/tree")
        tree = console.export_text(clear=True)
        assert parent in tree and "└─" in tree and "◀" in tree
        await router.dispatch("/sessions")
        assert parent in console.export_text(clear=True)
        assert tl.events  # SESSION_BRANCHED recorded
        rows = [e for e in tl.store.events(parent) if e["type"] == EventType.SESSION_BRANCHED.value]
        assert rows and rows[0]["data"]["child"] == child
        await router.dispatch(f"/resume {parent}")
        assert tl.session_id == parent and len(tl.context.messages) == n_parent
    finally:
        await tl.stop()


# -- plan gate -------------------------------------------------------------------------------------
def _edit_provider():
    return ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="p",
                        name="task",
                        arguments={"action": "plan", "titles": ["Raise TIMEOUT"]},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="w",
                        name="write_file",
                        arguments={"path": "src/app.py", "content": "TIMEOUT = 60\n"},
                    )
                ]
            ),
            ModelResponse(text="done; validation not possible here"),
        ]
    )


async def test_plan_gate_blocks_until_approved_and_refuses_when_denied(
    project: Path, _trendlab_home: Path
):
    from trendlab.config.schema import PlanGateConfig

    tl = _tl(project, _edit_provider(), plan_gate=PlanGateConfig(enabled=True))
    await tl.start(interactive=False)
    try:
        assert tl.engine.unsafe  # the gate works even with approval prompts off
        task = asyncio.create_task(tl.run_prompt("raise the timeout"))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if tl.approvals.pending():
                break
        pend = tl.approvals.pending()
        assert len(pend) == 1 and pend[0].tool == "plan" and "Raise TIMEOUT" in pend[0].preview
        assert pend[0].remote_allowed and not pend[0].session_scope_allowed
        assert (project / "src" / "app.py").read_text() == "TIMEOUT = 30\n"  # nothing written yet
        tl.approvals.decide(pend[0].approval_id, "approve", "once", via="local", trusted=True)
        result = await task
        assert result.status == "COMPLETED"
        assert (project / "src" / "app.py").read_text() == "TIMEOUT = 60\n"
        assert tl.plan_gate.approved
        # Second run: denied → the write is refused and the run stops with a clear reason.
        (project / "src" / "app.py").write_text("TIMEOUT = 30\n")
        tl.gateway.register("scripted:m", _edit_provider())
        task = asyncio.create_task(tl.run_prompt("again"))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if tl.approvals.pending():
                break
        assert not tl.plan_gate.approved  # reset per run
        tl.approvals.decide(
            tl.approvals.pending()[0].approval_id, "deny", "once", via="local", trusted=True
        )
        result = await task
        assert result.status == "FAILED" and "plan rejected" in (result.stop_reason or "")
        assert (project / "src" / "app.py").read_text() == "TIMEOUT = 30\n"
        types = [e["type"] for e in tl.store.events(tl.session_id)]
        assert EventType.PLAN_APPROVED.value in types and EventType.PLAN_REJECTED.value in types
        console = Console(record=True, width=120, force_terminal=False)
        await CommandRouter(tl, console).dispatch("/plan gate off")
        assert "off" in console.export_text(clear=True) and not tl.plan_gate.enabled
    finally:
        await tl.stop()


async def test_plan_gate_off_by_default_never_asks(project: Path, _trendlab_home: Path):
    tl = _tl(project, _edit_provider())
    await tl.start(interactive=False)
    try:
        result = await tl.run_prompt("raise the timeout")
        assert result.status == "COMPLETED" and tl.remote_status()["plan_gate"]["enabled"] is False
    finally:
        await tl.stop()


# -- telegram buttons ------------------------------------------------------------------------------
class FakeTelegram:
    """Minimal Bot API: records calls, serves queued updates to getUpdates."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self._mid = 100

    def transport(self) -> httpx.MockTransport:
        def handler(req: httpx.Request) -> httpx.Response:
            method = req.url.path.rsplit("/", 1)[-1]
            payload = json.loads(req.content or b"{}")
            self.calls.append((method, payload))
            if method == "sendMessage":
                self._mid += 1
                return httpx.Response(200, json={"ok": True, "result": {"message_id": self._mid}})
            if method == "getUpdates":
                offset = payload.get("offset") or 0
                due = [u for u in self.updates if u["update_id"] >= offset]
                return httpx.Response(200, json={"ok": True, "result": due})
            return httpx.Response(200, json={"ok": True, "result": True})

        return httpx.MockTransport(handler)

    def sent(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]


def _button(payload: dict, label_prefix: str) -> str:
    for row in payload["reply_markup"]["inline_keyboard"]:
        for b in row:
            if b["text"].startswith(label_prefix):
                return b["callback_data"]
    raise AssertionError(f"no button {label_prefix}")


async def test_telegram_channel_buttons_decide_with_hmac_and_chat_check(
    manager_factory, events, recorder, monkeypatch
):
    monkeypatch.setenv("TRENDLAB_TELEGRAM_BOT_TOKEN", "123:abc")
    fake = FakeTelegram()
    cfg = TelegramNotificationConfig(chat_id="-5499")
    mgr = manager_factory()
    ch = TelegramChannel(
        cfg,
        events,
        project_name="demo",
        client=httpx.AsyncClient(transport=fake.transport()),
        poll_timeout=0,
        poll_interval=0.01,
    )
    await mgr.add_channel(ch)
    perm = PermissionRequest(
        tool="shell",
        category=OperationCategory.SHELL_WRITE,
        summary="Run build",
        command="make",
        cwd="/p",
        explanation="build it",
    )
    task = asyncio.create_task(mgr.request(perm))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if fake.sent("sendMessage"):
            break
    msg = fake.sent("sendMessage")[0]
    assert msg["chat_id"] == "-5499" and "Run build" in msg["text"] and "make" in msg["text"]
    assert "123:abc" not in json.dumps(msg)
    req = mgr.pending()[0]
    once = _button(msg, "✅ Approve once")
    assert parse_callback(once) == (req.approval_id, "once", once.rsplit("|", 1)[-1])
    assert len(once.encode()) <= 64  # Telegram's callback_data limit
    # 1) wrong chat → ignored + audited
    fake.updates.append(
        {
            "update_id": 1,
            "callback_query": {
                "id": "c1",
                "from": {"id": 7},
                "message": {"message_id": 101, "chat": {"id": "999"}},
                "data": once,
            },
        }
    )
    # 2) forged MAC → refused
    forged = once.rsplit("|", 1)[0] + "|deadbeefdeadbeef"
    fake.updates.append(
        {
            "update_id": 2,
            "callback_query": {
                "id": "c2",
                "from": {"id": 7},
                "message": {"message_id": 101, "chat": {"id": "-5499"}},
                "data": forged,
            },
        }
    )
    # 3) genuine deny → applied
    deny = _button(msg, "⛔ Deny")
    fake.updates.append(
        {
            "update_id": 3,
            "callback_query": {
                "id": "c3",
                "from": {"id": 42},
                "message": {"message_id": 101, "chat": {"id": "-5499"}},
                "data": deny,
            },
        }
    )
    result = await asyncio.wait_for(task, 5)
    assert (
        result.status == ApprovalStatus.DENIED and result.via == "telegram" and result.by == "tg:42"
    )
    codes = [e.data.get("code") for e in recorder.of_type(EventType.APPROVAL_REJECTED)]
    assert "wrong_chat" in codes and "invalid_token" in codes
    answers = fake.sent("answerCallbackQuery")
    assert any("Not authorized" in a["text"] for a in answers)
    assert any("token mismatch" in a["text"] for a in answers)
    for _ in range(100):
        await asyncio.sleep(0.01)
        if fake.sent("editMessageText"):
            break
    assert "Denied" in fake.sent("editMessageText")[0]["text"]  # buttons withdrawn
    # Replaying the deny button after the decision is refused.
    fake.updates.append(
        {
            "update_id": 4,
            "callback_query": {
                "id": "c4",
                "from": {"id": 42},
                "message": {"message_id": 101, "chat": {"id": "-5499"}},
                "data": deny,
            },
        }
    )
    for _ in range(100):
        await asyncio.sleep(0.01)
        if len(fake.sent("answerCallbackQuery")) >= 4:
            break
    assert "no longer pending" in fake.sent("answerCallbackQuery")[-1]["text"]
    # Approve-once path with the session button hidden when not allowed.
    task = asyncio.create_task(mgr.request(perm, can_persist=False))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(fake.sent("sendMessage")) >= 2:
            break
    msg2 = fake.sent("sendMessage")[1]
    assert all(
        not b["text"].startswith("✅ Session")
        for row in msg2["reply_markup"]["inline_keyboard"]
        for b in row
    )
    fake.updates.append(
        {
            "update_id": 5,
            "callback_query": {
                "id": "c5",
                "from": {"id": 42},
                "message": {"message_id": 102, "chat": {"id": "-5499"}},
                "data": _button(msg2, "✅ Approve once"),
            },
        }
    )
    result = await asyncio.wait_for(task, 5)
    assert result.status == ApprovalStatus.APPROVED and result.scope.value == "once"
    assert ch.status()["polling"]
    await mgr.remove_channel(ch)
    assert not ch.status()["polling"]


async def test_telegram_question_options_and_text_reply(manager_factory, events, monkeypatch):
    monkeypatch.setenv("TRENDLAB_TELEGRAM_BOT_TOKEN", "123:abc")
    fake = FakeTelegram()
    mgr = manager_factory()
    ch = TelegramChannel(
        TelegramNotificationConfig(chat_id="5"),
        events,
        client=httpx.AsyncClient(transport=fake.transport()),
        poll_timeout=0,
        poll_interval=0.01,
    )
    await mgr.add_channel(ch)
    task = asyncio.create_task(mgr.ask("Which DB?", ["sqlite", "postgres"]))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if fake.sent("sendMessage"):
            break
    msg = fake.sent("sendMessage")[0]
    assert "Which DB?" in msg["text"] and _button(msg, "postgres")
    fake.updates.append(
        {
            "update_id": 1,
            "callback_query": {
                "id": "q1",
                "from": {"id": 1},
                "message": {"message_id": 101, "chat": {"id": "5"}},
                "data": _button(msg, "postgres"),
            },
        }
    )
    res = await asyncio.wait_for(task, 5)
    assert res.status == ApprovalStatus.APPROVED and res.reason == "postgres"
    # Free-text reply to the question message.
    task = asyncio.create_task(mgr.ask("Name the branch?"))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(fake.sent("sendMessage")) >= 2:
            break
    fake.updates.append(
        {
            "update_id": 2,
            "message": {
                "message_id": 500,
                "from": {"id": 1},
                "chat": {"id": "5"},
                "text": "feature/db",
                "reply_to_message": {"message_id": 102},
            },
        }
    )
    res = await asyncio.wait_for(task, 5)
    assert res.reason == "feature/db" and res.via == "telegram"
    await mgr.remove_channel(ch)


async def test_telegram_channel_needs_token_and_chat(manager_factory, events, monkeypatch):
    import pytest

    from trendlab.approvals.channels.base import ChannelError

    monkeypatch.delenv("TRENDLAB_TELEGRAM_BOT_TOKEN", raising=False)
    mgr = manager_factory()
    with pytest.raises(ChannelError, match="bot token"):
        await TelegramChannel(TelegramNotificationConfig(chat_id="5"), events).start(mgr)
    monkeypatch.setenv("TRENDLAB_TELEGRAM_BOT_TOKEN", "t")
    with pytest.raises(ChannelError, match="chat_id"):
        await TelegramChannel(TelegramNotificationConfig(), events).start(mgr)


async def test_app_enables_telegram_channel_with_remote(
    project: Path, _trendlab_home: Path, monkeypatch
):
    monkeypatch.setenv("TRENDLAB_TELEGRAM_BOT_TOKEN", "123:abc")
    tl = _tl(project, ScriptedProvider([ModelResponse(text="ok")]))
    tl.config.remote_approval = RemoteApprovalConfig(enabled=False, port=0, telegram=True)
    tl.config.notifications = NotificationsConfig(
        enabled=True, provider="telegram", telegram=TelegramNotificationConfig(chat_id="9")
    )
    await tl.start(interactive=False)
    try:
        assert tl.approvals.notifier is None  # no duplicate plain notification
        await tl.enable_remote(persist=False)
        assert tl.telegram_channel is not None and tl.remote_status()["telegram"]["chat_id"] == "9"
        await tl.disable_remote(persist=False)
        assert tl.telegram_channel is None
    finally:
        await tl.stop()


async def test_plan_rejection_with_a_reason_becomes_codesign(project: Path, _trendlab_home: Path):
    """U30: 'no, keep it at 45' is direction, not a stop: the agent revises and asks again."""
    from trendlab.config.schema import PlanGateConfig

    def write(i, value):
        return ModelResponse(
            tool_calls=[
                ToolCall(
                    id=f"w{i}",
                    name="write_file",
                    arguments={"path": "src/app.py", "content": f"TIMEOUT = {value}\n"},
                )
            ]
        )

    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="p",
                        name="task",
                        arguments={"action": "plan", "titles": ["Raise TIMEOUT"]},
                    )
                ]
            ),
            write(1, 60),
            write(2, 45),
            ModelResponse(text="Set the timeout to 45 as asked."),
        ]
    )
    from trendlab.config.schema import PlannerConfig

    # no planner here: in tests it would share the scripted provider with the agent
    tl = _tl(
        project,
        provider,
        plan_gate=PlanGateConfig(enabled=True),
        planner=PlannerConfig(enabled=False),
    )
    await tl.start(interactive=False)

    async def next_pending():
        for _ in range(300):
            await asyncio.sleep(0.01)
            if tl.approvals.pending():
                return tl.approvals.pending()[0]
        raise AssertionError("no approval request")

    try:
        task = asyncio.create_task(tl.run_prompt("raise the timeout"))
        first = await next_pending()
        tl.approvals.decide(
            first.approval_id,
            "deny",
            "once",
            via="local",
            trusted=True,
            reason="keep the timeout at 45, not 60",
        )
        second = await next_pending()  # the gate asks again before the revised change
        assert second.approval_id != first.approval_id
        tl.approvals.decide(second.approval_id, "approve", "once", via="local", trusted=True)
        result = await task
    finally:
        await tl.stop()
    assert result.status == "COMPLETED"
    assert (project / "src" / "app.py").read_text() == "TIMEOUT = 45\n"
    said = [m for m in tl.agent.messages if "keep the timeout at 45" in str(m.get("content"))]
    assert said  # the person's words reached the agent
