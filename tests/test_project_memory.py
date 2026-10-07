"""Project memory across sessions (spec §92.1)."""

from pathlib import Path

from rich.console import Console

from trendlab.app import TrendLabApp
from trendlab.config.loader import load_config
from trendlab.config.schema import PermissionMode, RemoteApprovalConfig
from trendlab.context.project_memory import (
    ProjectMemory,
    deterministic_facts,
    noteworthy,
    parse_facts,
)
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.telemetry.events import EventType
from trendlab.ui.commands import CommandRouter


def test_memory_store_dedupes_caps_and_renders(tmp_path: Path):
    mem = ProjectMemory(tmp_path, max_entries=3, max_prompt_chars=200)
    assert mem.add("Tests run with `pytest -q` from the repo root")
    assert not mem.add(
        "tests run with pytest -q from the repo root."
    )  # same fact, different casing/punct
    assert not mem.add("short")
    assert mem.add("Lint with ruff before committing") and mem.add("Config lives in ~/.trendlab")
    assert mem.add("Fourth fact pushes the oldest out")
    assert len(mem.facts) == 3 and mem.facts[0].startswith("Lint with ruff")
    again = ProjectMemory(tmp_path)
    assert again.facts == mem.facts  # persisted
    assert again.forget(1) == "Lint with ruff before committing" and len(again.facts) == 2
    prompt = again.render_for_prompt()
    assert "Project memory" in prompt and "Fourth fact" in prompt
    assert again.clear() == 2 and again.render_for_prompt() == ""


def test_parse_and_fallback_facts():
    assert parse_facts("NONE") == []
    assert parse_facts(
        "- Run tests with pytest\n* Use ruff\nnot a bullet\n- [2026-10-06] dated"
    ) == ["Run tests with pytest", "Use ruff", "dated"]
    ev = {
        "validation_runs": [{"command": "pytest -q", "ok": True}, {"command": "mypy", "ok": False}],
        "steering": ["always add a test"],
        "changed_files": ["a.py"],
        "failures": 0,
    }
    assert noteworthy(ev) and not noteworthy(
        {"changed_files": [], "validation_runs": [], "steering": [], "failures": 0}
    )
    facts = deterministic_facts(ev)
    assert facts == [
        "Validation that passes here: `pytest -q`",
        "User asked during a run: always add a test",
    ]


def _tl(project, provider):
    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    return TrendLabApp(
        project,
        cfg,
        provider=provider,
        model_ref="scripted:m",
        permission_mode=PermissionMode.UNSAFE,
        console=Console(record=True, width=100, force_terminal=False),
    )


async def test_run_learns_facts_and_next_session_sees_them(project: Path, _trendlab_home: Path):
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="w",
                        name="write_file",
                        arguments={"path": "src/new.py", "content": "x = 1\n"},
                    )
                ]
            ),
            ModelResponse(tool_calls=[ToolCall(id="t", name="run_tests", arguments={})]),
            ModelResponse(text="added new.py and ran the tests"),
            # the summarizer call for memory extraction:
            ModelResponse(
                text="- Tests run with `python3 -c 'print(1)'` (the configured test command)\n- Source lives under src/\nNONE"
            ),
        ]
    )
    tl = _tl(project, provider)
    await tl.start(interactive=False)
    try:
        seen = []
        tl.events.subscribe(lambda e: seen.append(e))
        r = await tl.run_prompt("add a module and test it")
        assert r.status == "COMPLETED"
        await tl.wait_for_learning()
        mem = tl.memory
        assert mem is not None and len(mem.facts) == 2 and "Source lives under src/" in mem.facts
        assert (project / ".trendlab" / "memory.md").read_text().count("- [") == 2
        upd = [e for e in seen if e.type == EventType.MEMORY_UPDATED]
        assert upd and upd[0].data["total"] == 2
        assert (
            "Project memory" in tl.context.system_prompt
            and "Source lives under src/" in tl.context.system_prompt
        )
        # The summarizer prompt carried the existing memory and evidence.
        learn_call = provider.calls[-1][-1]["content"]
        assert "Run evidence" in learn_call and "src/new.py" in learn_call
    finally:
        await tl.stop()
    # A new session on the same project starts with the facts in its system prompt.
    tl2 = _tl(project, ScriptedProvider([ModelResponse(text="hi")]))
    await tl2.start(interactive=False)
    try:
        assert "Source lives under src/" in tl2.context.system_prompt
        sent = None
        await tl2.run_prompt("hello")
        sent = tl2.gateway.provider("scripted:m").calls[0][0]["content"]
        assert "Project memory" in sent
    finally:
        await tl2.stop()


async def test_quiet_runs_do_not_learn_and_commands_edit_memory(
    project: Path, _trendlab_home: Path
):
    provider = ScriptedProvider(
        [ModelResponse(text="just an answer"), ModelResponse(text="- should not be stored")]
    )
    tl = _tl(project, provider)
    await tl.start(interactive=False)
    try:
        await tl.run_prompt("what time is it?")
        await tl.wait_for_learning()
        assert tl.memory.facts == [] and len(provider.calls) == 1  # no summarizer call
        console = Console(record=True, width=120, force_terminal=False)
        router = CommandRouter(tl, console)
        await router.dispatch("/memory")
        assert "nothing remembered" in console.export_text(clear=True)
        await router.dispatch("/remember Deploys go through make release, never by hand")
        await router.dispatch("/remember Deploys go through make release, never by hand")
        out = console.export_text(clear=True)
        assert "remembered:" in out and "already remembered" in out
        assert "make release" in tl.context.system_prompt
        await router.dispatch("/memory")
        assert "1. Deploys go through make release" in console.export_text(clear=True)
        await router.dispatch("/memory forget 1")
        assert "forgot:" in console.export_text(clear=True) and tl.memory.facts == []
        assert "make release" not in tl.context.system_prompt
    finally:
        await tl.stop()
