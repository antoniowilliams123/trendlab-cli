"""Uplift U7: task graph, dependency-honouring execution, plan review, divergence replanning,
planning eval."""

import json
from pathlib import Path

from rich.console import Console

from trendlab.agent.planner import apply_steps, lint_plan, parse_steps
from trendlab.agent.tasks import Plan, TaskStatus
from trendlab.app import TrendLabApp
from trendlab.benchmarks.runner import plan_metrics, summarize, summarize_planning
from trendlab.benchmarks.suite import get_task
from trendlab.config.loader import load_config
from trendlab.config.schema import PermissionMode, RemoteApprovalConfig
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.telemetry.events import EventBus, EventType
from trendlab.tools.base import ToolContext
from trendlab.tools.task_tool import TaskInput, TaskTool

from .test_agent_runtime import make_agent

GRAPH = json.dumps(
    {
        "steps": [
            {
                "title": "Add the model",
                "files": ["src/model.py"],
                "done_when": "imports",
                "validation": None,
                "depends_on": [],
            },
            {
                "title": "Add the CLI flag",
                "files": ["src/cli.py"],
                "done_when": "flag parses",
                "validation": None,
                "depends_on": [],
            },
            {
                "title": "Wire model into CLI",
                "files": ["src/cli.py", "src/model.py"],
                "done_when": "flag uses model",
                "validation": None,
                "depends_on": [1, 2],
            },
            {
                "title": "Run tests",
                "files": [],
                "done_when": "green",
                "validation": "pytest -q",
                "depends_on": [3, 9],
            },
        ]
    }
)


def _plan():
    plan = Plan()
    apply_steps(plan, parse_steps(GRAPH))
    return plan


def test_task_graph_waves_ready_and_parallel_groups():
    plan = _plan()
    t1, t2, t3, t4 = plan.tasks
    assert t3.dependencies == ["T-1", "T-2"] and t4.dependencies == ["T-3"]  # 9 is dropped
    assert plan.waves() == [["T-1", "T-2"], ["T-3"], ["T-4"]]
    assert plan.parallel_safe() == [["T-1", "T-2"]]  # same wave, disjoint files
    assert plan.graph_issues() == []
    assert [t.id for t in plan.ready()] == ["T-2"]  # T-1 is active, T-3 waits for both
    assert "T-3 Wire model into CLI (after T-1, T-2)" in plan.render()
    t3.dependencies.append("T-4")  # T-4 already needs T-3 → cycle
    assert plan.graph_issues() == ["dependency cycle"]
    t3.dependencies = ["T-9", "T-3"]
    assert set(plan.graph_issues()) == {"T-3 depends on unknown T-9", "T-3 depends on itself"}


def test_failed_step_blocks_its_dependents():
    plan = _plan()
    assert plan.block_dependents("T-1") == ["T-3", "T-4"]
    assert plan.get("T-2").status != TaskStatus.BLOCKED
    assert plan.get("T-4").status == TaskStatus.BLOCKED


async def test_task_tool_honours_dependencies(project: Path):
    plan = _plan()
    tool = TaskTool(plan, EventBus())
    ctx = ToolContext(project_root=project, session_id="s")
    refused = await tool.run(TaskInput(action="update", task_id="T-3"), ctx)
    assert not refused.ok and "depends on T-1, T-2" in refused.output
    done = await tool.run(TaskInput(action="complete", task_id="T-1", evidence="ok"), ctx)
    assert done.ok and plan.active.id == "T-2"  # next READY step, not the next in the list
    await tool.run(TaskInput(action="complete", task_id="T-2", evidence="ok"), ctx)
    assert plan.active.id == "T-3"
    # completing out of order is allowed but called out
    plan2 = _plan()
    tool2 = TaskTool(plan2, EventBus())
    early = await tool2.run(TaskInput(action="complete", task_id="T-3", evidence="x"), ctx)
    assert early.ok and early.output.startswith("Note: T-3 was completed before T-1, T-2")
    failed = await tool2.run(TaskInput(action="fail", task_id="T-1", evidence="no"), ctx)
    # completed work is not undone: T-3 stays done, so T-4 (which needs only T-3) is not blocked
    assert failed.ok and plan2.get("T-3").status == TaskStatus.COMPLETED
    assert plan2.get("T-4").status == TaskStatus.PENDING


def test_plan_lint_is_a_pre_implementation_review():
    steps = parse_steps(GRAPH)
    exists = {"src/model.py", "src/cli.py"}.__contains__
    assert lint_plan(steps, exists=exists, validation={"test": "pytest -q"}) == [
        "step 4 depends on step 9, which is not earlier"
    ]
    steps[0]["files"] = ["src/ghost.py"]
    steps[0]["title"] = "Fix the ghost"  # an edit, not a creation
    steps[1]["done_when"] = ""
    steps[-1]["validation"] = None
    issues = lint_plan(steps, exists=exists, validation={"test": "pytest -q"})
    assert any("src/ghost.py which does not exist" in i for i in issues)
    assert "step 2 has no done_when condition" in issues
    assert "the last step must run the project's validation" in issues
    steps[0]["title"] = "Create the ghost module"  # creating a new file is fine
    assert not any("ghost" in i for i in lint_plan(steps, exists=exists))
    assert lint_plan([]) == ["the plan has no steps"]


async def test_planner_call_repairs_a_flawed_plan_once(project: Path, _trendlab_home: Path):
    cfg = load_config(project)
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    bad = {"steps": [{"title": "Fix it", "files": ["src/nope.py"], "done_when": "works"}]}
    good = {"steps": [{"title": "Fix it", "files": ["src/app.py"], "done_when": "works"}]}
    provider = ScriptedProvider(
        [ModelResponse(text=json.dumps(bad)), ModelResponse(text=json.dumps(good))]
    )
    tl = TrendLabApp(
        project,
        cfg,
        provider=provider,
        model_ref="scripted:m",
        permission_mode=PermissionMode.UNSAFE,
        console=Console(quiet=True),
    )
    await tl.start(interactive=False)
    try:
        steps = await tl._plan_steps("fix the timeout")
        review = tl.agent.plan_review
        # a clean plan costs no repair call
        provider._responses.append(ModelResponse(text=json.dumps(good)))  # noqa: SLF001
        await tl._plan_steps("fix the timeout")
        clean = tl.agent.plan_review
    finally:
        await tl.stop()
    assert steps[0]["files"] == ["src/app.py"]
    assert review["repaired"] and review["issues"] and review["remaining"] == []
    assert clean == {"issues": [], "remaining": [], "repaired": False}


async def test_divergence_from_the_plan_triggers_one_replan(
    project: Path, manager_factory, events, recorder
):
    def write(i, path):
        return ModelResponse(
            tool_calls=[
                ToolCall(
                    id=f"w{i}", name="write_file", arguments={"path": path, "content": f"v = {i}\n"}
                )
            ]
        )

    provider = ScriptedProvider(
        [
            write(1, "src/app.py"),
            write(2, "src/other.py"),
            write(3, "src/third.py"),
            write(4, "src/fourth.py"),
            ModelResponse(text="done"),
        ]
    )
    agent, tools = make_agent(
        project, manager_factory(), events, provider, mode=PermissionMode.UNSAFE
    )
    tools.registry.register(TaskTool(agent.plan, events))
    notes = []

    async def planner(task_text, note):
        notes.append(note)
        return [
            {"title": "Change app", "files": ["src/app.py"], "done_when": "", "validation": None},
            {"title": "Wrap up", "files": [], "done_when": "", "validation": None},
        ]

    agent.planner = planner
    await agent.run("Change src/app.py and update README.md accordingly " * 10)
    assert len(notes) == 2 and notes[0] == ""
    assert "src/other.py, src/third.py" in notes[1]  # replanned once, with the evidence
    called = recorder.of_type(EventType.PLANNER_CALLED)
    assert [e.data["reason"] for e in called] == ["initial", "divergence"]
    assert called[0].data["waves"] and called[0].data["graph_issues"] == []


def test_planning_metrics_per_run_and_summary():
    task = get_task("mt04-below_threshold")  # expected shop/stock.py, allowed shop/report.py
    events = [
        {
            "steps": 3,
            "files": [["shop/stock.py"], ["shop/util.py"], ["tests/test_stock.py"]],
            "dependencies": [[], ["T-1"], ["T-2"]],
            "graph_issues": [],
            "review": {"issues": ["x"], "remaining": []},
        },
        {"steps": 2, "replan": True, "files": [["shop/report.py"]]},
    ]
    m = plan_metrics(task, events, {"shop/stock.py", "shop/report.py", "shop/pricing.py"})
    # planned non-test: stock, util, report → 2 of 3 needed; changed non-test: 2 of 3 planned
    assert m["plan_precision"] == 0.667 and m["plan_adherence"] == 0.667
    assert m["plan_dependencies"] == 2 and m["plan_graph_ok"] and m["replans"] == 1
    assert m["plan_lint_issues"] == 1 and m["plan_lint_remaining"] == 0
    assert plan_metrics(task, [], set()) == {}
    rows = [
        {"task": "a", "passes": True, "cost": 0.0, "status": "COMPLETED", "plan_recall": 1.0, **m},
        {"task": "b", "passes": True, "cost": 0.0, "status": "COMPLETED"},
    ]
    p = summarize(rows)["planning"]
    assert p["plans"] == 1 and p["precision"] == 0.667 and p["replans"] == 1
    s = summarize_planning(
        [
            {
                "planned": True,
                "recall": 1.0,
                "precision": 0.5,
                "plans_tests": True,
                "final_validation": True,
                "graph_ok": True,
                "dependencies": 2,
                "lint_issues": ["x"],
                "lint_remaining": [],
                "cost": 0.001,
            },
            {"planned": False, "cost": 0.0005},
        ]
    )
    assert s["planned"] == 1 and s["lint_clean_after_review"] == 1.0 and s["cost"] == 0.0015
