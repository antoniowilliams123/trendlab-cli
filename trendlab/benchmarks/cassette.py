"""Deterministic replay (uplift U20): record every model response, replay sessions for free.

Recording: the gateway hands every response — lead and every auxiliary role (planner,
verifier, router, reviewer) — to a recorder that appends it to
``~/.trendlab/cassettes/<session>.jsonl``.

Replay (``trendlab replay <session> --deterministic``): the project is rebuilt as it was
*before* the session (the session's checkpoints hold every touched file's prior content), the
gateway serves responses from the cassette in order per role (no model call, $0), file tools
run for real in the scratch copy, and commands / web calls return their recorded output. The
harness itself is the only thing that can differ, so the report is a regression test of the
harness against a real session: same model calls per role, same tool sequence, same outcome —
or the first point where it diverged.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from trendlab.providers.base import ModelResponse, StreamChunk, ToolCall
from trendlab.tools.base import ToolResult

RECORDED_TOOLS = {"shell", "run_tests", "background", "web_fetch", "web_search", "ask_user"}


def cassette_dir() -> Path:
    from trendlab.config.loader import trendlab_home

    return trendlab_home() / "cassettes"


class Recorder:
    def __init__(self, session_id_fn) -> None:
        self.session_id_fn = session_id_fn  # the app's session id can change on /resume

    def __call__(self, role: str, model: str, response: ModelResponse) -> None:
        path = cassette_dir() / f"{self.session_id_fn()}.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {"role": role, "model": model, "response": response.model_dump(mode="json")}
                    )
                    + "\n"
                )
        except OSError:
            pass  # recording must never break a run


def prune_cassettes(store) -> int:
    """Delete cassettes whose session no longer exists (retention follows the sessions)."""
    d = cassette_dir()
    if not d.is_dir():
        return 0
    n = 0
    for f in d.glob("*.jsonl"):
        if store.get_session(f.stem) is None:
            f.unlink(missing_ok=True)
            n += 1
    return n


def load(session_id: str) -> list[dict[str, Any]]:
    path = cassette_dir() / f"{session_id}.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def recorded_tool_outputs(
    messages: list[dict[str, Any]], events: list[dict[str, Any]] | None = None
) -> dict[str, deque]:
    """Per tool name, the recorded (output, ok, exit_code) in call order, for commands and web
    calls. Outputs come from the transcript, ok/exit codes from the tool.completed events."""
    names: dict[str, str] = {}
    outputs: dict[str, list[str]] = defaultdict(list)
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                names[tc.get("id", "")] = (tc.get("function") or {}).get("name", "")
        elif m.get("role") == "tool":
            name = names.get(m.get("tool_call_id", ""), "")
            if name in RECORDED_TOOLS:
                outputs[name].append(str(m.get("content") or ""))
    status: dict[str, list[tuple[bool, Any]]] = defaultdict(list)
    for e in events or []:
        d = e.get("data") or {}
        if e.get("type") == "tool.completed" and d.get("tool") in RECORDED_TOOLS:
            status[d["tool"]].append((bool(d.get("ok", True)), d.get("exit_code")))
    out: dict[str, deque] = defaultdict(deque)
    for name, outs in outputs.items():
        st = status.get(name, [])
        for k, text in enumerate(outs):
            ok, code = st[k] if k < len(st) else (True, None)
            out[name].append((text, ok, code))
    return out


def install_recorded_tools(registry, recorded: dict[str, deque], divergences: list[str]) -> None:
    """Commands and web calls answer from the recording *inside* the tool, so the runtime still
    does its bookkeeping (validation runs, events) exactly as in the original session."""
    for name in RECORDED_TOOLS:
        tool = registry.get(name)
        if tool is None:
            continue

        async def run(args, ctx, _name=name):
            q = recorded.get(_name)
            if not q:
                divergences.append(f"{_name} has no recorded output left")
                return ToolResult(ok=False, output="(no recorded output)")
            text, ok, code = q.popleft()
            data: dict[str, Any] = {"replayed": True}
            if code is not None:
                data["exit_code"] = code
            return ToolResult(ok=ok, output=text, data=data)

        tool.run = run  # type: ignore[method-assign]


def restore_pre_session(store, session_id: str, root: Path) -> list[str]:
    """Roll the touched files in ``root`` back to their state before the session, using the
    session's checkpoint snapshots (the first snapshot of a file is its original content)."""
    src_root = Path((store.get_session(session_id) or {}).get("project_path") or "")
    restored, seen = [], set()
    for cp in sorted(store.checkpoints(session_id), key=lambda c: c["created_at"]):
        for e in cp["files"]:
            rel = e["path"]
            if rel in seen:
                continue
            seen.add(rel)
            dest = root / rel
            if e.get("pre_sha256") is None:
                dest.unlink(missing_ok=True)  # the session created it
            else:
                snap = src_root / ".trendlab" / "checkpoints" / cp["id"] / rel
                if snap.is_file():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(snap, dest)
            restored.append(rel)
    return restored


class Tape:
    """Serves recorded responses in order per role and notes every divergence."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.queues: dict[str, deque] = defaultdict(deque)
        for r in records:
            self.queues[r["role"]].append(ModelResponse.model_validate(r["response"]))
        self.served = 0
        self.divergences: list[str] = []

    def next(self, role: str) -> ModelResponse:
        q = self.queues.get(role)
        if not q:
            self.divergences.append(
                f"call {self.served + 1}: harness asked the {role} role, "
                "which the recording has no more answers for"
            )
            return ModelResponse(text="(cassette exhausted)", finish_reason="stop")
        self.served += 1
        return q.popleft()

    def leftover(self) -> dict[str, int]:
        return {role: len(q) for role, q in self.queues.items() if q}


class DeterministicTools:
    """Counts the tool sequence; every call goes through the real runtime (file tools run for
    real, recorded tools answer from the recording inside the tool)."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.calls: list[str] = []
        self.divergences: list[str] = []

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call.name)
        return await self.runtime.execute(call)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.runtime, name)


async def deterministic_replay(
    store, session_id: str, *, config, home: Path | None = None
) -> dict[str, Any]:
    from rich.console import Console

    from trendlab.app import TrendLabApp
    from trendlab.benchmarks.replay import copy_project, copy_working_set, load_session
    from trendlab.benchmarks.unattended import Unattended
    from trendlab.config.schema import PermissionMode

    records = load(session_id)
    if not records:
        return {"session": session_id, "error": "no cassette recorded for this session"}
    rec = load_session(store, session_id)
    messages = store.messages(session_id)
    events = store.events(session_id)
    before_status = [
        e["type"].split(".", 1)[1]
        for e in events
        if e["type"] in {"run.completed", "run.failed", "run.canceled"}
    ]
    src = Path(rec.project_path) if rec.project_path else None
    with tempfile.TemporaryDirectory(prefix="trendlab-replay-") as tmp:
        root = Path(tmp) / "repo"
        if src is not None and src.is_dir():
            if copy_project(src, root):
                shutil.rmtree(root, ignore_errors=True)
                copy_working_set(src, root, rec)
        else:
            root.mkdir()
        restored = restore_pre_session(store, session_id, root)
        config.defaults.permission_mode = PermissionMode.UNSAFE
        config.sessions.record_cassettes = False  # do not record the replay itself
        tl = TrendLabApp(
            root, config, model_ref=rec.model or None, console=Console(quiet=True), data_dir=home
        )
        Unattended(tl)
        await tl.start(interactive=False)
        tape = Tape(records)

        async def complete(model_ref, msgs, tools=None, *, role=None):
            return tape.next(role or "main"), model_ref

        async def stream(model_ref, msgs, tools=None, *, role=None):
            yield StreamChunk(final=tape.next(role or "main"))

        tl.gateway.complete = complete  # type: ignore[method-assign]
        tl.gateway.stream = stream  # type: ignore[method-assign]
        # no model is ever called; providers are only asked for their capabilities
        from trendlab.providers.scripted import ScriptedProvider

        tl.gateway._factory = lambda ref: ScriptedProvider([])  # noqa: SLF001
        tl.gateway._providers.clear()  # noqa: SLF001
        tools = DeterministicTools(tl.tools)
        install_recorded_tools(
            tl.tools.registry, recorded_tool_outputs(messages, events), tools.divergences
        )
        tl.agent.tools = tools  # type: ignore[assignment]
        started = time.monotonic()
        after_status, stop_reasons = [], []
        try:
            for prompt in rec.prompts:
                result = await tl.run_prompt(prompt)
                after_status.append(result.status.lower())
                if result.stop_reason:
                    stop_reasons.append(result.stop_reason)
        finally:
            await tl.stop()
    old_tools = [n for n, _ in rec.tool_calls]
    first_tool_diff = next(
        (i + 1 for i, (a, b) in enumerate(zip(old_tools, tools.calls, strict=False)) if a != b),
        None if len(old_tools) == len(tools.calls) else min(len(old_tools), len(tools.calls)) + 1,
    )
    divergences = tape.divergences + tools.divergences
    leftover = tape.leftover()
    identical = (
        not divergences
        and not leftover
        and first_tool_diff is None
        and (before_status == after_status)
    )
    return {
        "session": session_id,
        "identical": identical,
        "model_calls_recorded": len(records),
        "model_calls_served": tape.served,
        "unused_recorded_calls": leftover,
        "tool_calls_before": len(old_tools),
        "tool_calls_after": len(tools.calls),
        "first_tool_divergence": first_tool_diff,
        "outcomes_before": before_status,
        "outcomes_after": after_status,
        "divergences": divergences[:10],
        "stop_reasons": stop_reasons,
        "files_restored": len(restored),
        "cost": 0.0,
        "wall_s": round(time.monotonic() - started, 1),
    }
