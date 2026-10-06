"""Guards for weak/local models: ~ in tool paths, and empty or JSON-only final answers."""

from pathlib import Path

import pytest

from trendlab.agent.evaluator import CompletionEvaluator, EvidenceSummary, _substantive
from trendlab.agent.tasks import Plan
from trendlab.config.schema import PermissionMode
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.tools.base import PathOutsideProjectError, ToolContext

from .test_agent_runtime import make_agent


def test_tilde_paths_resolve_inside_project(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    ctx = ToolContext(project_root=proj, session_id="s")
    assert ctx.resolve("~/proj/src") == (proj / "src").resolve()
    # Home itself as the project root (Tony's usual way of starting from ~).
    home_ctx = ToolContext(project_root=tmp_path, session_id="s")
    assert home_ctx.resolve("~/proj/src") == (proj / "src").resolve()
    with pytest.raises(PathOutsideProjectError):
        ctx.resolve("~/elsewhere")
    assert ctx.resolve("src") == (proj / "src").resolve()


def test_substantive_answers():
    assert not _substantive("") and not _substantive("{}") and not _substantive("[]")
    assert not _substantive('{"ok": true}') and not _substantive("null") and not _substantive("...")
    assert _substantive("The folder has 6 files.") and _substantive("{not json but words}")


async def test_json_only_answer_is_nudged_then_real_answer_accepted(
    project, manager_factory, events
):
    mgr = manager_factory()
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="l", name="list_directory", arguments={"path": "nope"})]
            ),
            ModelResponse(text="{}"),
            ModelResponse(text="The folder 'nope' does not exist; src/ contains app.py."),
        ]
    )
    agent, _ = make_agent(project, mgr, events, provider, mode=PermissionMode.UNSAFE)
    result = await agent.run("list nope")
    assert result.status == "COMPLETED" and result.text.startswith("The folder 'nope'")
    nudges = [m for m in agent.messages if m["role"] == "user" and "just JSON" in str(m["content"])]
    assert len(nudges) == 1
    ev = CompletionEvaluator()
    assert not ev.evaluate(EvidenceSummary([], [], Plan()), "{}", validation_available=False).accept
