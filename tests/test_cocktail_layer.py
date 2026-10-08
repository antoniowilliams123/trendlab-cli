"""Model cocktail, attribution, per-model prompt layer, guards and skill triggers (spec §6)."""

from pathlib import Path

from trendlab.agent.runtime import infer_phase
from trendlab.config.schema import AppConfig, PermissionMode, ProviderConfig
from trendlab.extensions.skills import SkillLibrary
from trendlab.prompts.drivers import driver_layers, driver_text, model_slug
from trendlab.providers.base import ModelResponse, TokenUsage, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.telemetry.costs import CostTracker
from trendlab.telemetry.events import EventType

from .test_agent_runtime import make_agent


def test_driver_layers_merge_in_order(tmp_path: Path):
    cfg = AppConfig(
        providers={"deepseek": ProviderConfig(), "anthropic": ProviderConfig(type="anthropic")}
    )
    home, proj = tmp_path / "home", tmp_path / "proj"
    assert model_slug("deepseek:deepseek-flash") == "deepseek-deepseek-flash"
    layers = driver_layers(cfg, "deepseek:deepseek-flash", proj, home)
    assert (
        layers[0].name == "openai_compatible.md" and layers[1].name == "deepseek-deepseek-flash.md"
    )
    text = driver_text(cfg, "deepseek:deepseek-flash", proj, home)
    assert text.startswith("\n\n## Model notes (deepseek:deepseek-flash)")
    assert "Flash driver profile" in text and "tool-call channel" in text
    (proj / ".trendlab/skills/_model").mkdir(parents=True)
    (proj / ".trendlab/skills/_model/deepseek-deepseek-flash.md").write_text(
        "Project note: run make test."
    )
    text2 = driver_text(cfg, "deepseek:deepseek-flash", proj, home)
    assert text2.endswith("Project note: run make test.") and "Flash driver profile" in text2
    assert "Model notes" in driver_text(cfg, "anthropic:claude-sonnet-5", proj, home)
    assert (
        driver_text(AppConfig(providers={"x": ProviderConfig(type="weird")}), "x:m", proj, home)
        == ""
    )


def test_phase_inference_and_cost_attribution():
    assert infer_phase(["read_file", "glob"]) == "explore"
    assert infer_phase(["read_file", "patch_file"]) == "edit"
    assert infer_phase(["run_tests"]) == "validate"
    assert infer_phase(["task"]) == "plan"
    assert infer_phase([]) == "other"
    costs = CostTracker(AppConfig())
    costs.record(
        "m",
        TokenUsage(input_tokens=10, output_tokens=1),
        0,
        role="main",
        phase="edit",
        step_id="T-1",
        attempt=1,
    )
    costs.record("m", TokenUsage(input_tokens=5, output_tokens=1), 0, role="verifier")
    costs.record("m", TokenUsage(input_tokens=5, output_tokens=1), 0, role="planner")
    assert costs.by("phase")["edit"]["calls"] == 1 and costs.by("phase")["verify"]["input"] == 5
    assert costs.by("role")["planner"]["calls"] == 1 and costs.by("step")["T-1"]["calls"] == 1
    assert costs.by("step")["(no step)"]["calls"] == 2


async def test_lead_calls_carry_phase_and_guards_fire(
    project: Path, manager_factory, events, recorder
):
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="r", name="read_file", arguments={"path": "README.md"})]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="w",
                        name="write_file",
                        arguments={"path": "src/x.py", "content": "x = 1\n"},
                    )
                ]
            ),
            ModelResponse(text="I will now run the tests."),  # announces instead of acting → guard
            ModelResponse(tool_calls=[ToolCall(id="t", name="run_tests", arguments={})]),
            ModelResponse(text="done"),
        ]
    )
    agent, _ = make_agent(project, manager_factory(), events, provider, mode=PermissionMode.UNSAFE)
    result = await agent.run("add x.py")
    assert result.status == "COMPLETED"
    phases = [r.phase for r in agent.costs.records]
    assert phases == ["plan", "explore", "edit", "other", "validate"]
    guards = [e.data["guard"] for e in recorder.of_type(EventType.GUARD_FIRED)]
    assert "announced_action" in guards and all(
        e.data["model"] == "scripted:m" for e in recorder.of_type(EventType.GUARD_FIRED)
    )


def _skill(root: Path, name: str, toml: str, body: str = "Do the thing carefully.") -> None:
    d = root / ".trendlab/skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"# {name}\n{body}\n")
    (d / "skill.toml").write_text(toml)


async def test_skill_triggers_load_once_per_run(
    project: Path, manager_factory, events, recorder, tmp_path: Path
):
    _skill(project, "migrations", '[triggers]\nkeywords = ["migration"]\npaths = ["db/*.sql"]\n')
    _skill(project, "flaky", '[triggers]\non_failure = ["timeout"]\ntools = ["run_tests"]\n')
    lib = SkillLibrary(project, tmp_path / "home")
    assert [s.name for s, t in lib.match(task_text="write a migration")] == ["migrations"]
    assert [t for s, t in lib.match(paths=["db/001.sql"])] == ["path"]
    assert [(s.name, t) for s, t in lib.match(failure="Error: timeout after 30s")] == [
        ("flaky", "on_failure")
    ]
    assert lib.match(task_text="nothing relevant") == []
    provider = ScriptedProvider(
        [
            ModelResponse(tool_calls=[ToolCall(id="t", name="run_tests", arguments={})]),
            ModelResponse(tool_calls=[ToolCall(id="t2", name="run_tests", arguments={})]),
            ModelResponse(text="ran twice; nothing to change"),
        ]
    )
    agent, _ = make_agent(project, manager_factory(), events, provider, mode=PermissionMode.UNSAFE)
    agent.skills = lib
    result = await agent.run("add a migration for users")
    assert result.status == "COMPLETED"
    loaded = [(e.data["name"], e.data["trigger"]) for e in recorder.of_type(EventType.SKILL_LOADED)]
    assert loaded == [
        ("migrations", "keyword"),
        ("flaky", "tool"),
    ]  # each once, despite two run_tests
    assert [e.data["name"] for e in recorder.of_type(EventType.SKILL_UNLOADED)] == [
        "flaky",
        "migrations",
    ]
    injected = [
        m
        for m in agent.messages
        if m.get("role") == "user" and "Skill 'migrations' applies" in str(m.get("content"))
    ]
    assert len(injected) == 1
