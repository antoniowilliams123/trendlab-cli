"""Verify-then-surface (cheap-model spec §3.2, §5): verifier verdicts, fix rounds, regression
gate, worktree workspace."""

import subprocess
from pathlib import Path

from rich.console import Console

from trendlab.agent.runtime import is_test_file, looks_like_fix, regression_outcome
from trendlab.agent.verifier import VerifierVerdict, build_messages, parse_verdict
from trendlab.app import TrendLabApp
from trendlab.config.loader import load_config
from trendlab.config.schema import PermissionMode, RemoteApprovalConfig
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.telemetry.events import EventType
from trendlab.ui.activity import run_footer

from .test_agent_runtime import make_agent

WRITE = ModelResponse(
    tool_calls=[
        ToolCall(id="w", name="write_file", arguments={"path": "src/x.py", "content": "x = 2\n"})
    ]
)
TEST = ModelResponse(tool_calls=[ToolCall(id="t", name="run_tests", arguments={})])
DONE = ModelResponse(text="Fixed the bug in src/x.py; tests pass.")


def test_parse_verdict_is_strict_and_tolerant_of_prose():
    assert parse_verdict("no json here") is None
    assert parse_verdict('{"verdict": "maybe"}') is None
    v = parse_verdict(
        'Sure. {"verdict": "fix", "findings": [{"file": "a.py", "line": "12", "issue": "off by one",'
        ' "severity": "HIGH"}, {"bogus": 1}], "regression_test": "missing"}'
    )
    assert v.verdict == "fix" and v.findings == [
        {"file": "a.py", "line": 12, "issue": "off by one", "severity": "high"}
    ]
    assert v.regression_test == "missing"
    fb = v.feedback()
    assert "a.py:12: off by one" in fb and "regression test is missing" in fb
    assert v.footer() == "verifier asked for fixes (1 finding)"


def test_build_messages_elides_huge_diffs_and_includes_validation():
    msgs = build_messages(
        task="fix it",
        diff="+" * 200_000,
        validation={"command": "pytest", "ok": True, "exit_code": 0, "tail": "1 passed"},
        plan="",
    )
    body = msgs[0]["content"]
    assert (
        len(body) < 70_000 and "[diff elided]" in body and "1 passed" in body and "fix it" in body
    )


def test_fix_heuristics():
    assert looks_like_fix("Fix the crash when the list is empty")
    assert looks_like_fix("tests are failing after the upgrade")
    assert not looks_like_fix("Add a --json flag to the export command")
    assert (
        is_test_file("tests/test_x.py")
        and is_test_file("src/x_test.go")
        and is_test_file("a.spec.ts")
    )
    assert not is_test_file("src/x.py")
    assert regression_outcome(["src/x.py", "tests/test_x.py"], "") == "present"
    assert (
        regression_outcome(["src/x.py"], "done. regression test: not applicable: docs") == "waived"
    )
    assert regression_outcome(["src/x.py"], "done") == "missing"
    assert regression_outcome(["tests/test_x.py"], "") == "not_applicable"
    # the existing suite failed before the fix and passed after it → the test already exists
    runs = [{"ok": False}, {"ok": True}]
    assert regression_outcome(["src/x.py"], "", runs) == "present"
    assert regression_outcome(["src/x.py"], "", [{"ok": True}, {"ok": True}]) == "missing"
    assert regression_outcome(["src/x.py"], "", [{"ok": True}, {"ok": False}]) == "missing"


def _verifier(verdicts):
    calls = []

    async def fake(task_text, ev):
        calls.append((task_text, list(ev.changed_files)))
        v = verdicts.pop(0)
        return v

    fake.calls = calls
    return fake


async def test_pass_verdict_surfaces_run(project: Path, manager_factory, events, recorder):
    provider = ScriptedProvider([WRITE, TEST, DONE])
    agent, _ = make_agent(project, manager_factory(), events, provider, mode=PermissionMode.UNSAFE)
    agent.verifier = _verifier([VerifierVerdict("pass", model="strong:m")])
    agent.verification_mode = "required"
    result = await agent.run("fix the bug in x.py")
    assert result.status == "COMPLETED" and result.verification["verdict"] == "pass"
    ev = recorder.of_type(EventType.VERIFY_VERDICT)
    assert ev and ev[0].data["verdict"] == "pass" and ev[0].data["model"] == "strong:m"
    assert (
        agent.verifier.calls[0][1] == ["src/x.py"] and "fix the bug" in agent.verifier.calls[0][0]
    )
    assert "verified ✓" in run_footer(result)


async def test_fix_verdict_gives_author_one_round_then_pass(
    project: Path, manager_factory, events, recorder
):
    provider = ScriptedProvider(
        [WRITE, TEST, DONE, WRITE, TEST, ModelResponse(text="addressed the finding; done")]
    )
    agent, _ = make_agent(project, manager_factory(), events, provider, mode=PermissionMode.UNSAFE)
    agent.verifier = _verifier(
        [
            VerifierVerdict(
                "fix",
                findings=[
                    {"file": "src/x.py", "line": 1, "issue": "x should be 3", "severity": "high"}
                ],
            ),
            VerifierVerdict("pass"),
        ]
    )
    agent.verification_mode = "required"
    result = await agent.run("fix x")
    assert result.status == "COMPLETED" and "addressed" in result.text
    # the findings were handed to the author as a user message
    handed = [
        m
        for m in agent.messages
        if m.get("role") == "user" and "x should be 3" in str(m.get("content"))
    ]
    assert handed and len(agent.verifier.calls) == 2
    assert [e.data["verdict"] for e in recorder.of_type(EventType.VERIFY_VERDICT)] == [
        "fix",
        "pass",
    ]


async def test_fix_after_rounds_exhausted_surfaces_with_findings(
    project: Path, manager_factory, events
):
    finding = {"file": "src/x.py", "line": 1, "issue": "name it better", "severity": "low"}
    provider = ScriptedProvider([WRITE, TEST, DONE, WRITE, TEST, ModelResponse(text="done again")])
    agent, _ = make_agent(project, manager_factory(), events, provider, mode=PermissionMode.UNSAFE)
    agent.verifier = _verifier(
        [VerifierVerdict("fix", findings=[finding]), VerifierVerdict("fix", findings=[finding])]
    )
    agent.verification_mode = "required"
    result = await agent.run("fix x")
    assert result.status == "COMPLETED" and result.verification["verdict"] == "fix"
    assert "verifier: fix (1)" in run_footer(result)


async def test_fail_verdict_stops_required_but_not_advisory(project: Path, manager_factory, events):
    bad = VerifierVerdict(
        "fail",
        findings=[
            {"file": "src/x.py", "line": 1, "issue": "deletes user data", "severity": "high"}
        ],
    )
    agent, _ = make_agent(
        project,
        manager_factory(),
        events,
        ScriptedProvider([WRITE, TEST, DONE]),
        mode=PermissionMode.UNSAFE,
    )
    agent.verifier = _verifier([bad])
    agent.verification_mode = "required"
    result = await agent.run("fix x")
    assert (
        result.status == "FAILED"
        and "verifier rejected" in result.stop_reason
        and "deletes user data" in result.stop_reason
    )
    assert "verifier: fail (1)" in run_footer(result)
    agent2, _ = make_agent(
        project,
        manager_factory(),
        events,
        ScriptedProvider([WRITE, TEST, DONE]),
        mode=PermissionMode.UNSAFE,
    )
    agent2.verifier = _verifier([VerifierVerdict("fail", findings=bad.findings)])
    agent2.verification_mode = "advisory"
    result2 = await agent2.run("fix x")
    assert result2.status == "COMPLETED" and result2.verification["verdict"] == "fail"


async def test_verifier_failure_or_no_changes_never_blocks(
    project: Path, manager_factory, events, recorder
):
    async def broken(task_text, ev):
        raise RuntimeError("provider down")

    agent, _ = make_agent(
        project,
        manager_factory(),
        events,
        ScriptedProvider([WRITE, TEST, DONE]),
        mode=PermissionMode.UNSAFE,
    )
    agent.verifier = broken
    agent.verification_mode = "required"
    result = await agent.run("fix x")
    assert result.status == "COMPLETED" and result.verification == {"verdict": "unavailable"}
    assert recorder.of_type(EventType.VERIFY_VERDICT)[0].data["verdict"] == "unavailable"
    # a read-only run (no files changed) is never verified
    agent2, _ = make_agent(
        project,
        manager_factory(),
        events,
        ScriptedProvider([ModelResponse(text="The README says demo.")]),
        mode=PermissionMode.UNSAFE,
    )
    agent2.verifier = _verifier([])
    agent2.verification_mode = "required"
    result2 = await agent2.run("what does the README say?")
    assert result2.status == "COMPLETED" and result2.verification is None


async def test_regression_gate_nudges_fix_without_test(
    project: Path, manager_factory, events, recorder
):
    provider = ScriptedProvider(
        [
            WRITE,
            TEST,
            DONE,  # fix with no test → nudged
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="w2",
                        name="write_file",
                        arguments={
                            "path": "tests/test_x.py",
                            "content": "def test_x():\n    assert True\n",
                        },
                    )
                ]
            ),
            TEST,
            ModelResponse(text="added a regression test; done"),
        ]
    )
    agent, _ = make_agent(project, manager_factory(), events, provider, mode=PermissionMode.UNSAFE)
    agent.regression_gate = True
    result = await agent.run("fix the broken total in x.py")
    assert result.status == "COMPLETED" and "regression" in result.text
    nudges = [
        m
        for m in agent.messages
        if m.get("role") == "user" and "regression test" in str(m.get("content"))
    ]
    assert nudges
    gate = recorder.of_type(EventType.REGRESSION_GATE)
    assert gate and gate[-1].data["outcome"] == "present"
    # not a fix → no gate
    agent2, _ = make_agent(
        project,
        manager_factory(),
        events,
        ScriptedProvider([WRITE, TEST, DONE]),
        mode=PermissionMode.UNSAFE,
    )
    agent2.regression_gate = True
    r2 = await agent2.run("add a constant to x.py")
    assert r2.status == "COMPLETED" and len(recorder.of_type(EventType.REGRESSION_GATE)) == 1


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    ).stdout


async def test_worktree_workspace_applies_verified_diff_and_parks_failed(
    project: Path, _trendlab_home: Path
):
    _git(project, "init", "-q")
    _git(project, "add", "-A")
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
    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    cfg.verification.workspace = "worktree"
    cfg.verification.verifier = "off"
    provider = ScriptedProvider([WRITE, ModelResponse(text="wrote x.py")])
    tl = TrendLabApp(
        project,
        cfg,
        provider=provider,
        model_ref="scripted:m",
        permission_mode=PermissionMode.UNSAFE,
        console=Console(quiet=True),
    )
    seen = []
    tl.events.subscribe(lambda e: seen.append(e) if e.type == EventType.WORKTREE_RUN else None)
    await tl.start(interactive=False)
    try:
        result = await tl.run_prompt("add src/x.py")
    finally:
        await tl.stop()
    assert result.status == "COMPLETED"
    assert (project / "src/x.py").read_text() == "x = 2\n"  # applied to the main tree
    assert [e.data["outcome"] for e in seen] == ["entered", "applied"]
    assert (
        tl.project_root == project.resolve()
        and not (project / ".trendlab/worktrees").exists()
        or not list((project / ".trendlab/worktrees").glob("run-*"))
    )
    assert "trendlab/run-" not in _git(project, "branch", "--list")
    # a run the verifier rejects leaves the main tree untouched and parks the patch
    cfg.verification.verifier = "required"
    provider2 = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="w",
                        name="write_file",
                        arguments={"path": "src/y.py", "content": "y = 1\n"},
                    )
                ]
            ),
            ModelResponse(text="wrote y.py"),
        ]
    )
    tl2 = TrendLabApp(
        project,
        cfg,
        provider=provider2,
        model_ref="scripted:m",
        permission_mode=PermissionMode.UNSAFE,
        console=Console(quiet=True),
    )
    seen2 = []
    tl2.events.subscribe(lambda e: seen2.append(e) if e.type == EventType.WORKTREE_RUN else None)
    await tl2.start(interactive=False)
    tl2.agent.verification_mode = "required"
    tl2.agent.verifier = _verifier(
        [
            VerifierVerdict(
                "fail",
                findings=[{"file": "src/y.py", "line": 1, "issue": "wrong", "severity": "high"}],
            )
        ]
    )
    try:
        result2 = await tl2.run_prompt("add src/y.py")
    finally:
        await tl2.stop()
    assert result2.status == "FAILED" and not (project / "src/y.py").exists()
    parked = [e for e in seen2 if e.data["outcome"] == "parked"]
    assert parked and (project / parked[0].data["patch"]).read_text().startswith("diff --git")
