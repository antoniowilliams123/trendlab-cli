"""Step-scoped execution (cheap-model spec §3.3, §4): planner call, per-step validation and
iteration cap, best-of-N candidates."""

import subprocess
from pathlib import Path

from trendlab.agent.planner import apply_steps, needs_planner, parse_steps, plan_message
from trendlab.agent.tasks import Plan, TaskStatus
from trendlab.config.schema import AppConfig, PermissionMode
from trendlab.orchestration.candidates import CandidateRunner
from trendlab.permissions.engine import PermissionEngine
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.telemetry.costs import CostTracker
from trendlab.telemetry.events import EventType
from trendlab.tools.base import ToolContext
from trendlab.tools.registry import default_registry

from .test_agent_runtime import make_agent

PLAN_JSON = (
    '{"steps": [{"title": "Fix the total in orders.py", "files": ["orders.py"], '
    '"done_when": "order_total returns the sum", "validation": "python3 -c \'print(1)\'"}, '
    '{"title": "Run the suite", "files": [], "done_when": "all tests pass", "validation": null}]}'
)


def test_planner_heuristic_and_parsing():
    assert not needs_planner("fix the typo")
    assert needs_planner("fix the typo in app.py and update README.md")
    assert needs_planner("x" * 201)
    steps = parse_steps("Here you go:\n" + PLAN_JSON)
    assert [s["title"] for s in steps] == ["Fix the total in orders.py", "Run the suite"]
    assert steps[0]["validation"] == "python3 -c 'print(1)'" and steps[1]["validation"] is None
    assert parse_steps("nope") is None and parse_steps('{"steps": []}') is None
    plan = Plan()
    added = apply_steps(plan, steps)
    assert [t.id for t in added] == ["T-1", "T-2"] and plan.active.id == "T-1"
    assert added[0].validation and added[0].files == ["orders.py"]
    msg = plan_message(added)
    assert "Start with T-1" in msg and "validate with" in msg


def _task(action, **kw):
    return ModelResponse(
        tool_calls=[ToolCall(id=f"t-{action}", name="task", arguments={"action": action, **kw})]
    )


async def test_planner_runs_once_and_step_validation_gates_completion(
    project: Path, manager_factory, events, recorder
):
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="w",
                        name="write_file",
                        arguments={"path": "src/x.py", "content": "x = 2\n"},
                    )
                ]
            ),
            _task(
                "complete", task_id="T-1", evidence="changed x"
            ),  # validation fails → handed back
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="w2", name="write_file", arguments={"path": "src/ok", "content": "1"}
                    )
                ]
            ),
            _task("complete", task_id="T-1", evidence="now passes"),  # validation passes
            _task("complete", task_id="T-2", evidence="done"),
            ModelResponse(text="both steps done"),
        ]
    )
    agent, tools = make_agent(
        project, manager_factory(), events, provider, mode=PermissionMode.UNSAFE
    )
    from trendlab.tools.task_tool import TaskTool

    tools.registry.register(TaskTool(agent.plan, events))
    calls = []

    async def planner(task_text, note):
        calls.append(note)
        return [
            {
                "title": "Change x",
                "files": ["src/x.py"],
                "done_when": "src/ok exists",
                "validation": "test -f src/ok",
            },
            {"title": "Wrap up", "files": [], "done_when": "", "validation": None},
        ]

    agent.planner = planner
    result = await agent.run("Please fix src/x.py and also touch src/ok so the check passes " * 4)
    assert result.status == "COMPLETED" and calls == [""]
    planned = recorder.of_type(EventType.PLANNER_CALLED)
    assert planned and planned[0].data["steps"] == 2
    failed = [e for e in recorder.of_type(EventType.STEP_FAILED)]
    assert (
        failed
        and failed[0].data["reason"] == "validation_failed"
        and failed[0].data["attempt"] == 1
    )
    done = recorder.of_type(EventType.STEP_COMPLETED)
    assert [e.data["step"] for e in done] == ["T-1"]
    handed = [
        m
        for m in agent.messages
        if m.get("role") == "user" and "validation `test -f src/ok` failed" in str(m.get("content"))
    ]
    assert handed
    assert (
        agent.plan.get("T-1").status == TaskStatus.COMPLETED and agent.plan.get("T-1").attempts == 1
    )


async def test_iteration_cap_triggers_replan(project: Path, manager_factory, events, recorder):
    (project / "notes.txt").write_text("\n".join(f"line {i}" for i in range(1, 13)))
    reads = [
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id=f"r{i}",
                    name="read_file",
                    arguments={"path": "notes.txt", "start_line": i + 1, "end_line": i + 2},
                )
            ]
        )
        for i in range(6)
    ]
    provider = ScriptedProvider(
        reads + [_task("complete", task_id="T-2", evidence="ok"), ModelResponse(text="finished")]
    )
    agent, tools = make_agent(
        project, manager_factory(), events, provider, mode=PermissionMode.UNSAFE
    )
    from trendlab.tools.task_tool import TaskTool

    tools.registry.register(TaskTool(agent.plan, events))
    notes = []

    async def planner(task_text, note):
        notes.append(note)
        if not note:
            return [{"title": "Big step", "files": [], "done_when": "", "validation": None}]
        return [{"title": "Smaller step", "files": [], "done_when": "", "validation": None}]

    agent.planner = planner
    agent.step_iterations = 3
    result = await agent.run("do a long thing " * 20)
    assert result.status == "COMPLETED"
    assert len(notes) == 2 and "did not finish within 3 iterations" in notes[1]
    cap = [
        e for e in recorder.of_type(EventType.STEP_FAILED) if e.data["reason"] == "iteration_cap"
    ]
    assert cap and cap[0].data["step"] == "T-1"
    assert [e.data["replan"] for e in recorder.of_type(EventType.PLANNER_CALLED)] == [False, True]
    assert agent.plan.get("T-2").title == "Smaller step"


async def test_best_of_callback_finishes_failed_step(
    project: Path, manager_factory, events, recorder
):
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="w",
                        name="write_file",
                        arguments={"path": "src/x.py", "content": "x = 2\n"},
                    )
                ]
            ),
            _task("complete", task_id="T-1", evidence="changed x"),  # validation fails → best-of
            _task("complete", task_id="T-2", evidence="done"),
            ModelResponse(text="all done"),
        ]
    )
    agent, tools = make_agent(
        project, manager_factory(), events, provider, mode=PermissionMode.UNSAFE
    )
    from trendlab.tools.task_tool import TaskTool

    tools.registry.register(TaskTool(agent.plan, events))

    async def planner(task_text, note):
        return [
            {
                "title": "Change x",
                "files": ["src/x.py"],
                "done_when": "",
                "validation": "test -f src/ok",
            },
            {"title": "Wrap up", "files": [], "done_when": "", "validation": None},
        ]

    seen = []

    async def candidates(task, tail, n):
        seen.append((task.id, n))
        (project / "src/ok").write_text("1")  # the winning candidate's diff landed
        return {
            "applied": True,
            "winner": 2,
            "files": ["src/ok"],
            "candidates": [{"n": 1, "passed": False}, {"n": 2, "passed": True}],
        }

    agent.planner = planner
    agent.candidates = candidates
    agent.best_of = 3
    result = await agent.run("fix src/x.py and make src/ok appear " * 6)
    assert result.status == "COMPLETED" and seen == [("T-1", 3)]
    done = [e for e in recorder.of_type(EventType.STEP_COMPLETED) if e.data["step"] == "T-1"]
    assert done and done[0].data["via"] == "best_of" and done[0].data["winner"] == 2
    assert "src/ok" in result.changed_files
    assert any(
        "finished by a parallel attempt" in str(m.get("content"))
        for m in agent.messages
        if m.get("role") == "user"
    )


async def test_candidate_runner_picks_passing_patch(
    project: Path, manager_factory, events, recorder
):
    subprocess.run(["git", "-C", str(project), "init", "-q"], check=True)
    (project / "calc.py").write_text("def mul(a, b):\n    return a * b + 1\n")
    (project / "test_calc.py").write_text(
        "from calc import mul\n\ndef test_mul():\n    assert mul(3, 4) == 12\n"
    )
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-qm",
            "init",
        ],
        check=True,
    )
    mgr = manager_factory()
    bad = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="p",
                        name="patch_file",
                        arguments={"path": "calc.py", "old_text": "+ 1", "new_text": "+ 2"},
                    )
                ]
            ),
            ModelResponse(text="tried"),
        ]
    )
    good = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="p",
                        name="patch_file",
                        arguments={"path": "calc.py", "old_text": " + 1", "new_text": ""},
                    )
                ]
            ),
            ModelResponse(text="fixed"),
        ]
    )
    providers = [bad, good]
    cfg = AppConfig()
    ctx = ToolContext(project_root=project, session_id=mgr.session_id)
    runner = CandidateRunner(
        config=cfg,
        events=events,
        session_id=mgr.session_id,
        model_ref="scripted:m",
        registry=default_registry(),
        engine=PermissionEngine(PermissionMode.UNSAFE),
        approvals=mgr,
        costs=CostTracker(cfg),
        parent_ctx=ctx,
        provider_factory=lambda ref, k: providers[k],
    )
    plan = Plan()
    task = plan.add("fix mul")
    task.validation = "python3 -m pytest -q -p no:cacheprovider test_calc.py"
    task.attempts = 1
    out = await runner.run_best_of(task, "assert 13 == 12", 2)
    assert out["applied"] and out["winner"] == 2 and out["files"] == ["calc.py"]
    assert (project / "calc.py").read_text() == "def mul(a, b):\n    return a * b\n"
    cands = recorder.of_type(EventType.ATTEMPT_CANDIDATE)
    assert [c.data["passed"] for c in sorted(cands, key=lambda c: c.data["n"])] == [False, True]
    assert not list((project / ".trendlab/worktrees").glob("cand-*"))
    branches = subprocess.run(
        ["git", "-C", str(project), "branch", "--list"], capture_output=True, text=True
    ).stdout
    assert "cand-" not in branches
