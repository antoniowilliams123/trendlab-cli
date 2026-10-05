from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from trendlab.approvals.channels.web import WebApprovalChannel
from trendlab.approvals.manager import ApprovalManager
from trendlab.config.schema import RemoteApprovalConfig
from trendlab.permissions.engine import PermissionRequest
from trendlab.permissions.models import OperationCategory
from trendlab.sessions.store import SessionStore
from trendlab.telemetry.events import EventBus, EventRecorder

TOKEN = "test-token-" + "x" * 40


@pytest.fixture(autouse=True)
def _trendlab_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("TRENDLAB_HOME", str(home))
    # Tests use fake binaries and bare remotes under the host /tmp, which a sandboxed shell
    # cannot see; sandbox-specific tests opt back in with TRENDLAB_SANDBOX=on.
    monkeypatch.setenv("TRENDLAB_SANDBOX", "off")
    return home


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "README.md").write_text("# demo\n")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("TIMEOUT = 30\n")
    return root


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    s = SessionStore(tmp_path / "sessions.db")
    yield s
    s.close()


@pytest.fixture
def events() -> EventBus:
    return EventBus()


@pytest.fixture
def recorder(events: EventBus) -> EventRecorder:
    rec = EventRecorder()
    events.subscribe(rec)
    return rec


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_perm(
    command: str = "pip install pandas-ta",
    category: OperationCategory = OperationCategory.PACKAGE_INSTALL,
    cwd: str = "/work/trading-engine",
    tool: str = "shell",
    **kw,
) -> PermissionRequest:
    return PermissionRequest(
        tool=tool,
        category=category,
        summary=kw.pop("summary", "Install Python package"),
        command=command,
        cwd=cwd,
        args=kw.pop("args", {"command": command, "cwd": None, "timeout": 120}),
        **kw,
    )


@pytest.fixture
def manager_factory(store: SessionStore, events: EventBus, clock: FakeClock, project: Path):
    def _make(
        config: RemoteApprovalConfig | None = None,
        notifier=None,
        process_id: str | None = None,
        session_id: str | None = None,
    ) -> ApprovalManager:
        cfg = config or RemoteApprovalConfig(enabled=True, request_timeout_minutes=30)
        sid = session_id or store.create_session(str(project), "ThinkPad")
        return ApprovalManager(
            config=cfg,
            events=events,
            store=store,
            session_id=sid,
            machine="ThinkPad",
            project_name="trading-engine",
            notifier=notifier,
            approval_url="http://thinkpad:8787",
            clock=clock,
            process_id=process_id,
        )

    return _make


@pytest.fixture
async def web(manager_factory, events: EventBus):
    """A manager with a live web channel on an ephemeral loopback port."""
    cfg = RemoteApprovalConfig(enabled=True, host="127.0.0.1", port=0, request_timeout_minutes=30)
    manager = manager_factory(cfg)
    channel = WebApprovalChannel(cfg, TOKEN, events)
    await manager.add_channel(channel)
    client = httpx.Client(
        base_url=channel.url, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=5
    )
    try:
        yield manager, channel, client
    finally:
        client.close()
        await manager.remove_channel(channel)


async def run_request(manager: ApprovalManager, perm: PermissionRequest, **kw):
    """Start a request as a task and give the loop a tick so it is registered."""
    task = asyncio.create_task(manager.request(perm, **kw))
    for _ in range(20):
        await asyncio.sleep(0)
        if manager.pending():
            break
    return task
