"""Error paths are visible and recoverable (spec §91.3): retries on streams, auth hints,
question/approval lines without duplicates, overflow recovery while streaming."""

from pathlib import Path

from rich.console import Console

from trendlab.app import TrendLabApp
from trendlab.config.loader import load_config
from trendlab.config.schema import PermissionMode, RemoteApprovalConfig
from trendlab.providers.base import (
    ModelResponse,
    ProviderAuthenticationError,
    ProviderContextOverflowError,
    ProviderRateLimitError,
    StreamChunk,
    ToolCall,
)
from trendlab.providers.scripted import ScriptedProvider
from trendlab.telemetry.events import EventType
from trendlab.ui.activity import format_event
from trendlab.ui.repl import Repl


class Flaky(ScriptedProvider):
    """Raises ``exc`` for the first ``fail_times`` calls (complete and stream alike)."""

    def __init__(self, exc, responses, fail_times=1):
        super().__init__(responses)
        self.exc, self.fail_times, self.n = exc, fail_times, 0

    async def complete(self, messages, tools=None):
        self.n += 1
        if self.n <= self.fail_times:
            raise self.exc
        return await super().complete(messages, tools)

    async def stream(self, messages, tools=None):
        self.n += 1
        if self.n <= self.fail_times:
            raise self.exc
        resp = await ScriptedProvider.complete(self, messages, tools)
        if resp.text:
            yield StreamChunk(text=resp.text)
        yield StreamChunk(final=resp)


def _tl(project, provider, mode=PermissionMode.UNSAFE):
    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    cfg.retry.base_delay_seconds = 0.01
    cfg.retry.max_delay_seconds = 0.02
    console = Console(record=True, width=120, force_terminal=False)
    tl = TrendLabApp(
        project,
        cfg,
        provider=provider,
        model_ref="scripted:m",
        permission_mode=mode,
        console=console,
        on_token=lambda t: None,
    )
    return tl, console


async def test_stream_rate_limit_retries_then_succeeds(project: Path, _trendlab_home: Path):
    tl, console = _tl(
        project,
        Flaky(
            ProviderRateLimitError("scripted: rate limited"),
            [ModelResponse(text="recovered; nothing changed")],
        ),
    )
    await tl.start(interactive=False)
    try:
        tl.agent.stream = True
        seen = []
        tl.events.subscribe(lambda e: seen.append(e))
        r = await tl.run_prompt("go")
        assert r.status == "COMPLETED" and r.text.startswith("recovered")
        retries = [e for e in seen if e.type == EventType.PROVIDER_RETRY]
        assert len(retries) == 1 and retries[0].data["attempt"] == 1
        assert "retrying scripted:m" in format_event(retries[0])
        assert not [e for e in seen if e.type == EventType.PROVIDER_FALLBACK]
    finally:
        await tl.stop()


async def test_stream_overflow_compacts_and_recovers(project: Path, _trendlab_home: Path):
    tl, console = _tl(
        project,
        Flaky(
            ProviderContextOverflowError("scripted: context overflow"),
            [ModelResponse(text="recovered after compaction")],
        ),
    )
    await tl.start(interactive=False)
    try:
        tl.agent.stream = True
        r = await tl.run_prompt("go")
        assert r.status == "COMPLETED" and "recovered" in r.text
    finally:
        await tl.stop()


async def test_auth_failure_line_carries_the_fix(project: Path, _trendlab_home: Path):
    tl, console = _tl(
        project,
        Flaky(
            ProviderAuthenticationError("scripted: environment variable OPENAI_API_KEY is not set"),
            [ModelResponse(text="x")],
            fail_times=9,
        ),
    )
    await tl.start(interactive=False)
    try:
        seen = []
        tl.events.subscribe(lambda e: seen.append(e))
        r = await tl.run_prompt("go")
        assert r.status == "FAILED"
        lines = [format_event(e) for e in seen if e.type == EventType.RECOVERY]
        assert any(
            "authentication failed" in (ln or "")
            and "trendlab secret set OPENAI_API_KEY" in (ln or "")
            for ln in lines
        )
    finally:
        await tl.stop()


async def test_question_and_denial_lines_are_not_duplicated(
    project: Path, _trendlab_home: Path, manager_factory
):
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
    tl, console = _tl(project, provider)
    repl = Repl(tl, console)
    await tl.start(interactive=False)
    try:
        tl.events.subscribe(repl._on_event)
        import asyncio

        task = asyncio.create_task(tl.run_prompt("set up db"))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if tl.approvals.pending():
                break
        q = tl.approvals.pending()[0]
        assert q.kind == "question"
        tl.approvals.answer(q.approval_id, "sqlite", via="local", trusted=True)
        await task
        out = console.export_text()
        assert "Question: Which DB?" in out
        assert "approved at the keyboard" not in out  # a question is not an approval
        assert out.count("USER ANSWER") == 0  # no raw tool echo under the question
    finally:
        await tl.stop()
