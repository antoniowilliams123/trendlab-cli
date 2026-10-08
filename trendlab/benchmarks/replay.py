"""Shadow replay (cheap-model spec §8.6).

``trendlab replay <session-id>`` re-runs a stored session's user prompts against the current
build in a scratch copy of the project. Recorded tool results are fed back whenever the new
run makes a call with the same name and arguments (so unchanged exploration costs nothing and
is deterministic); other calls run live. The report diffs tool choices, transcript length,
cost and outcome against the original session.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trendlab.providers.base import ToolCall
from trendlab.tools.base import ToolResult

HARNESS_PREFIXES = (
    "Before finishing, address the following",
    "A step plan was prepared",
    "Step ",
    "Skill '",
    "An independent review of your change",
    "Plan prepared",
)


@dataclass
class RecordedSession:
    session_id: str
    project_path: str
    model: str
    prompts: list[str]
    tool_calls: list[tuple[str, str]]  # (name, canonical args) in order
    results: dict[tuple[str, str], str] = field(default_factory=dict)
    assistant_turns: int = 0
    cost_usd: float = 0.0


def canonical(name: str, arguments: dict[str, Any]) -> tuple[str, str]:
    return name, json.dumps(arguments, sort_keys=True, default=str)


def load_session(store, session_id: str) -> RecordedSession:
    meta = store.get_session(session_id) or {}
    messages = store.messages(session_id)
    prompts: list[str] = []
    calls: list[tuple[str, str]] = []
    results: dict[tuple[str, str], str] = {}
    pending: dict[str, tuple[str, str]] = {}
    turns = 0
    for m in messages:
        role = m.get("role")
        if role == "user":
            content = m.get("content")
            text = (
                content
                if isinstance(content, str)
                else " ".join(
                    str(p.get("text") or "") for p in (content or []) if isinstance(p, dict)
                )
            )
            if text and not text.startswith(HARNESS_PREFIXES):
                prompts.append(text)
        elif role == "assistant":
            turns += 1
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {"_raw": fn.get("arguments")}
                key = canonical(fn.get("name", ""), args)
                calls.append(key)
                pending[tc.get("id", "")] = key
        elif role == "tool":
            key = pending.get(m.get("tool_call_id", ""))
            if key is not None:
                results.setdefault(key, str(m.get("content") or ""))
    usage = {}
    try:
        usage = store.usage(session_id) or {}
    except Exception:  # noqa: BLE001
        usage = {}
    return RecordedSession(
        session_id=session_id,
        project_path=str(meta.get("project_path") or meta.get("project") or ""),
        model=str(meta.get("model") or ""),
        prompts=prompts,
        tool_calls=calls,
        results=results,
        assistant_turns=turns,
        cost_usd=float(usage.get("cost_usd") or usage.get("total_usd") or 0.0),
    )


class ReplayTools:
    """Wraps ``ToolRuntime.execute``: recorded results first, live execution otherwise."""

    def __init__(self, runtime, recorded: RecordedSession, events, session_id: str) -> None:
        self.runtime = runtime
        self.recorded = recorded
        self.events = events
        self.session_id = session_id
        self._execute = runtime.execute
        self.calls: list[tuple[str, str]] = []
        self.replayed = 0
        self.live = 0

    async def execute(self, call: ToolCall) -> ToolResult:
        key = canonical(call.name, call.arguments)
        self.calls.append(key)
        if key in self.recorded.results and call.name not in {"shell", "run_tests"}:
            self.replayed += 1
            return ToolResult(ok=True, output=self.recorded.results[key], data={"replayed": True})
        self.live += 1
        return await self._execute(call)

    def __getattr__(self, name: str) -> Any:  # everything else is the real runtime
        return getattr(self.runtime, name)


async def replay_session(
    store,
    session_id: str,
    *,
    config,
    model: str | None = None,
    max_prompts: int | None = None,
    home: Path | None = None,
    provider=None,
) -> dict[str, Any]:
    from rich.console import Console

    from trendlab.app import TrendLabApp
    from trendlab.config.schema import PermissionMode

    rec = load_session(store, session_id)
    if not rec.prompts:
        return {"session": session_id, "error": "no user prompts stored for this session"}
    src = Path(rec.project_path) if rec.project_path else None
    with tempfile.TemporaryDirectory(prefix="trendlab-replay-") as tmp:
        root = Path(tmp) / "repo"
        copy_mode = "full"
        if src is not None and src.is_dir():
            problem = copy_project(src, root)
            if problem:
                # Too big or the home directory: copy only the working set the session touched.
                shutil.rmtree(root, ignore_errors=True)
                n = copy_working_set(src, root, rec)
                if n == 0:
                    return {"session": session_id, "project": str(src), "error": problem}
                copy_mode = f"working set ({n} files)"
        else:
            root.mkdir()
        config.defaults.permission_mode = PermissionMode.UNSAFE
        tl = TrendLabApp(
            root,
            config,
            model_ref=model or rec.model or None,
            provider=provider,
            console=Console(quiet=True),
            data_dir=home,
        )
        from trendlab.benchmarks.unattended import Unattended

        Unattended(tl)  # nobody watches a replay: deny approvals, answer questions
        await tl.start(interactive=False)
        shim = ReplayTools(tl.tools, rec, tl.events, tl.session_id)
        tl.agent.tools = shim  # type: ignore[assignment]
        started = time.monotonic()
        outcomes = []
        try:
            for prompt in rec.prompts[: max_prompts or len(rec.prompts)]:
                result = await tl.run_prompt(prompt)
                outcomes.append(result.status)
        finally:
            await tl.stop()
        new_calls = shim.calls
        old_names = [n for n, _ in rec.tool_calls]
        new_names = [n for n, _ in new_calls]
        same_prefix = 0
        for a, b in zip(old_names, new_names, strict=False):
            if a != b:
                break
            same_prefix += 1
        return {
            "session": session_id,
            "project": rec.project_path,
            "model_before": rec.model,
            "model_after": tl.model_ref,
            "prompts": len(rec.prompts[: max_prompts or len(rec.prompts)]),
            "outcomes": outcomes,
            "tool_calls_before": len(rec.tool_calls),
            "tool_calls_after": len(new_calls),
            "tool_sequence_shared_prefix": same_prefix,
            "tools_before": _counts(old_names),
            "tools_after": _counts(new_names),
            "replayed_results": shim.replayed,
            "live_calls": shim.live,
            "assistant_turns_before": rec.assistant_turns,
            "assistant_turns_after": (tl.costs.model_calls if tl.costs else 0),
            "cost_before": round(rec.cost_usd, 4),
            "cost_after": round(tl.costs.total_usd if tl.costs else 0.0, 4),
            "wall_s": round(time.monotonic() - started, 1),
            "copy_mode": copy_mode,
        }


MAX_FILES = 20_000
MAX_BYTES = 500 * 1024 * 1024
_SKIP = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".trendlab",
    ".mypy_cache",
    ".ruff_cache",
}


def copy_project(src: Path, dest: Path) -> str | None:
    """Scratch copy for a replay: tracked files only when ``src`` is a git repo, otherwise a
    bounded walk. Returns a reason instead of copying a home directory or a data dump."""
    import subprocess

    src = src.resolve()
    if src in {Path.home().resolve(), Path("/")}:
        return "refusing to replay a session whose project is the home directory"
    if (src / ".git").exists():
        proc = subprocess.run(
            ["git", "-C", str(src), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            rels = [r for r in proc.stdout.decode("utf-8", "replace").split("\0") if r]
            if len(rels) > MAX_FILES:
                return f"project has {len(rels)} files; replay caps at {MAX_FILES}"
            total = 0
            for rel in rels:
                p = src / rel
                if not p.is_file():
                    continue
                total += p.stat().st_size
                if total > MAX_BYTES:
                    return f"project exceeds {MAX_BYTES // 1_048_576} MB; replay refused"
                d = dest / rel
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, d)
            return None
    count = total = 0
    for p in src.rglob("*"):
        if any(part in _SKIP for part in p.relative_to(src).parts):
            continue
        if p.is_file():
            count += 1
            total += p.stat().st_size
            if count > MAX_FILES or total > MAX_BYTES:
                return "project too large for a replay copy (cap 20k files / 500 MB)"
            d = dest / p.relative_to(src)
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, d)
    return None


_PATH_KEYS = ("path", "file_path", "file", "paths", "files")


def working_set(rec: RecordedSession) -> set[str]:
    """Paths the recorded session read or edited (from its tool-call arguments)."""
    out: set[str] = set()
    for _name, args_json in rec.tool_calls:
        try:
            args = json.loads(args_json)
        except ValueError:
            continue
        for key in _PATH_KEYS:
            v = args.get(key)
            for p in v if isinstance(v, list) else [v]:
                if isinstance(p, str) and p and not p.startswith(("http:", "https:")):
                    out.add(p)
    return out


def copy_working_set(
    src: Path, dest: Path, rec: RecordedSession, max_bytes: int = 50 * 1024 * 1024
) -> int:
    """Copy only the files the session touched (resolved under ``src``); directories are
    listed but not copied recursively. Returns the number of files copied."""
    dest.mkdir(parents=True, exist_ok=True)
    src = src.resolve()
    copied = total = 0
    for raw in sorted(working_set(rec)):
        p = Path(raw).expanduser()
        p = (p if p.is_absolute() else src / p).resolve()
        try:
            rel = p.relative_to(src)
        except ValueError:
            continue
        if any(part in _SKIP for part in rel.parts) or not p.is_file():
            continue
        size = p.stat().st_size
        if total + size > max_bytes:
            break
        d = dest / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, d)
        copied += 1
        total += size
    return copied


def _counts(names: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for n in names:
        out[n] = out.get(n, 0) + 1
    return dict(sorted(out.items()))
