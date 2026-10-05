import asyncio
import sys
from pathlib import Path

import pytest

from trendlab.config.schema import AppConfig, HookConfig, McpServerConfig, PermissionMode
from trendlab.extensions.hooks import HookRunner
from trendlab.extensions.init import detect_project, draft_instructions
from trendlab.extensions.mcp import McpManager
from trendlab.extensions.skills import SkillLibrary
from trendlab.orchestration.subagents import SubAgentRunner, SubAgentTask, delegate_tool_factory
from trendlab.permissions.engine import PermissionEngine
from trendlab.permissions.models import OperationCategory
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.gateway import ModelGateway
from trendlab.providers.scripted import ScriptedProvider
from trendlab.telemetry.costs import CostTracker
from trendlab.telemetry.events import EventType
from trendlab.tools.base import ToolContext
from trendlab.tools.registry import default_registry
from trendlab.tools.runtime import ToolRuntime


def _runner(project, mgr, events, providers: dict, config=None):
    config = config or AppConfig()
    engine = PermissionEngine(PermissionMode.ASK)
    ctx = ToolContext(
        project_root=project,
        session_id=mgr.session_id,
        validation_commands={"test": "python3 -c 'print(42)'"},
    )
    gw = ModelGateway(config, events, mgr.session_id, provider_factory=providers.__getitem__)
    costs = CostTracker(config)
    runner = SubAgentRunner(
        config=config,
        gateway=gw,
        events=events,
        session_id=mgr.session_id,
        session_model="main:m",
        parent_registry=default_registry(),
        parent_ctx=ctx,
        engine=engine,
        approvals=mgr,
        costs=costs,
    )
    return runner, gw, engine, ctx, costs


async def test_explorer_subagent_is_isolated_and_read_only(
    project, manager_factory, events, recorder
):
    mgr = manager_factory()
    explorer = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="1", name="search_text", arguments={"pattern": "TIMEOUT"})]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(id="2", name="write_file", arguments={"path": "x", "content": "y"})
                ]
            ),
            ModelResponse(text="SUMMARY: TIMEOUT defined in src/app.py\nFILES: src/app.py"),
        ]
    )
    cfg = AppConfig(routing={"explorer": "cheap:m"})
    runner, *_ = _runner(
        project, mgr, events, {"cheap:m": explorer, "main:m": ScriptedProvider([])}, cfg
    )
    report = await runner.run(
        SubAgentTask(objective="Where is TIMEOUT defined?", context="user asked")
    )
    assert report.status == "COMPLETED" and report.model == "cheap:m" and report.role == "explorer"
    assert "src/app.py" in report.findings and report.model_calls == 3 and report.tool_calls == 1
    assert "unknown tool: write_file" in explorer.calls[2][-1]["content"]  # writes not available
    assert not (project / "x").exists()
    sys_prompt = explorer.calls[0][0]["content"]
    assert (
        "EXPLORER" in sys_prompt and "user asked" in sys_prompt and "src/app.py" not in sys_prompt
    )
    assert len(explorer.calls[0]) == 2  # system + objective only: no parent transcript
    spawned = recorder.of_type(EventType.AGENT_SPAWNED)[0].data
    assert spawned["role"] == "explorer" and recorder.of_type(EventType.AGENT_COMPLETED)


async def test_delegate_tool_parallel_and_cost_rollup(project, manager_factory, events):
    mgr = manager_factory()
    from trendlab.config.schema import ModelPricing
    from trendlab.providers.base import TokenUsage

    cfg = AppConfig(
        pricing={"main:m": ModelPricing(output_per_million=1_000_000.0)}
    )  # $1 per output token
    provider = ScriptedProvider(
        [ModelResponse(text=f"report {i}", usage=TokenUsage(output_tokens=1)) for i in range(3)]
    )
    runner, gw, engine, ctx, costs = _runner(project, mgr, events, {"main:m": provider}, cfg)
    tool = delegate_tool_factory(runner)
    args = tool.parse({"objective": "A", "role": "debugger", "parallel_objectives": ["B", "C"]})
    perm = tool.permission(args, ctx)
    assert perm.category == OperationCategory.READ_ONLY and "Delegate to debugger" in perm.summary
    res = await tool.run(args, ctx)
    assert res.ok and res.output.count("[") >= 3 and len(res.data["reports"]) == 3
    assert {r["role"] for r in res.data["reports"]} == {"debugger", "explorer"}
    assert costs.total_usd == pytest.approx(3.0)  # sub-agent spend rolled into the session


async def test_tester_role_can_run_tests(project, manager_factory, events):
    mgr = manager_factory()
    tester = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="1", name="run_tests", arguments={"kind": "test"})]
            ),
            ModelResponse(text="RESULTS: 42 printed; pass"),
        ]
    )
    runner, *_ = _runner(project, mgr, events, {"main:m": tester})
    report = await runner.run(
        SubAgentTask(objective="validate", role="tester", allowed_tools=["run_tests", "read_file"])
    )
    assert report.status == "COMPLETED" and "42" in tester.calls[1][-1]["content"]


async def test_hooks_run_and_block(project, events, recorder):
    (project / "log.txt").write_text("")
    hooks = [
        HookConfig(
            event="before_write", command="echo $TRENDLAB_FILES >> log.txt; exit 1", blocking=True
        ),
        HookConfig(event="after_tool", command="echo after:$TRENDLAB_TOOL >> log.txt"),
        HookConfig(event="session_start", command="sleep 5", timeout_seconds=1),
    ]
    runner = HookRunner(hooks, project, events, "s")
    veto = await runner.run("session_start")
    assert veto is None
    assert recorder.of_type(EventType.HOOK_RUN)[0].data["exit_code"] == 124

    class Perm:
        command = ""
        affected_files = ["src/app.py"]

    class Res:
        ok = True

    veto = await runner.before_tool("write_file", Perm())
    assert veto and "exited 1" in veto and recorder.of_type(EventType.HOOK_BLOCKED)
    await runner.after_tool("read_file", Perm(), Res())
    log = (project / "log.txt").read_text()
    assert '["src/app.py"]' in log and "after:read_file" in log


async def test_hooks_integrated_with_tool_runtime(project, manager_factory, events):
    mgr = manager_factory()
    engine = PermissionEngine(PermissionMode.AUTO_EDIT)
    ctx = ToolContext(project_root=project, session_id=mgr.session_id)
    rt = ToolRuntime(default_registry(), engine, mgr, events, ctx)
    rt.hooks = HookRunner(
        [HookConfig(event="before_write", command="exit 3", blocking=True)], project, events, "s"
    )
    res = await rt.execute(
        ToolCall(id="1", name="write_file", arguments={"path": "blocked.py", "content": "x"})
    )
    assert not res.ok and "BLOCKED by hook" in res.output and not (project / "blocked.py").exists()
    res = await rt.execute(ToolCall(id="2", name="read_file", arguments={"path": "src/app.py"}))
    assert res.ok  # non-write tools unaffected


def test_skills_library(project, _trendlab_home):
    skill_dir = _trendlab_home / "skills" / "quant-research"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Quant research\nAlways vectorize with numpy.\n")
    (skill_dir / "skill.toml").write_text(
        'description = "Quant workflows"\nvalidation = ["pytest -q"]\n'
    )
    proj_skill = project / ".trendlab" / "skills" / "local-style"
    proj_skill.mkdir(parents=True)
    (proj_skill / "SKILL.md").write_text("# Local style\nUse tabs.\n")
    (project / ".trendlab" / "skills" / "not-a-skill").mkdir()
    lib = SkillLibrary(project, _trendlab_home)
    names = [s.name for s in lib.list()]
    assert names == ["quant-research", "local-style"]
    assert lib.get("quant-research").description == "Quant workflows" and lib.get(
        "quant-research"
    ).validation == ["pytest -q"]
    assert lib.get("local-style").description == "Local style"
    lib.activate("quant-research")
    prompt = lib.apply("BASE")
    assert prompt.startswith("BASE") and "Skill: quant-research" in prompt and "vectorize" in prompt
    assert lib.apply(prompt).count("## Active skills") == 1  # idempotent
    lib.deactivate("quant-research")
    assert lib.apply(prompt) == "BASE"


def test_init_detection_and_draft(project):
    (project / "pyproject.toml").write_text("[tool.ruff]\n[tool.mypy]\n[tool.pytest.ini_options]\n")
    (project / "tests").mkdir()
    (project / "package.json").write_text(
        '{"scripts": {"build": "tsc"}, "devDependencies": {"jest": "1", "eslint": "1"}}'
    )
    (project / "tsconfig.json").write_text("{}")
    info = detect_project(project)
    assert info["languages"] == ["Python", "TypeScript"] and "pytest" in info["test_frameworks"]
    assert (
        "ruff check" in info["linters"]
        and "mypy" in info["type_checkers"]
        and "tsc" in info["type_checkers"]
    )
    assert info["build"] == ["npm run build"]
    draft = draft_instructions(project, {"test": "python -m pytest -q"})
    assert draft.startswith("# TrendLab Project Instructions") and "`python -m pytest -q`" in draft
    assert "Never disable or delete tests" in draft


async def test_mcp_tools_register_and_pass_through_permissions(
    project, manager_factory, events, recorder
):
    mgr = manager_factory()
    server = McpServerConfig(
        command=sys.executable,
        args=[str(Path(__file__).parent / "fake_mcp_server.py")],
        category="read_only",
    )
    broken = McpServerConfig(command="/definitely/not/a/binary")
    manager = McpManager({"fake": server, "broken": broken}, events, mgr.session_id)
    registry = default_registry()
    await manager.start(registry)
    try:
        status = manager.status()
        assert status["fake"]["tools"] == ["echo", "boom"] and status["broken"][
            "status"
        ].startswith("failed")
        assert recorder.of_type(EventType.MCP_SERVER_STARTED) and recorder.of_type(
            EventType.MCP_SERVER_FAILED
        )
        engine = PermissionEngine(PermissionMode.ASK)
        ctx = ToolContext(project_root=project, session_id=mgr.session_id)
        rt = ToolRuntime(registry, engine, mgr, events, ctx)
        res = await rt.execute(
            ToolCall(id="1", name="mcp_fake_echo", arguments={"text": "hi there"})
        )
        assert res.ok and res.output == "echo: hi there"
        res = await rt.execute(ToolCall(id="2", name="mcp_fake_boom", arguments={}))
        assert not res.ok and "kaboom" in res.output
        res = await rt.execute(ToolCall(id="3", name="mcp_fake_echo", arguments={}))
        assert not res.ok and "invalid arguments" in res.output  # required field enforced
        # A server configured as 'network' asks for permission in ask mode.
        tool = registry.get("mcp_fake_echo")
        tool.category = OperationCategory.NETWORK
        task = asyncio.create_task(
            rt.execute(ToolCall(id="4", name="mcp_fake_echo", arguments={"text": "x"}))
        )
        for _ in range(100):
            await asyncio.sleep(0.01)
            if mgr.pending():
                break
        [req] = mgr.pending()
        assert req.summary == "MCP fake: echo" and req.risk.value == "medium"
        mgr.decide(req.approval_id, "deny", via="local", trusted=True)
        assert "NOT EXECUTED" in (await task).output
    finally:
        await manager.stop()
