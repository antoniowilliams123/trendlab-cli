import subprocess
from pathlib import Path

import pytest

from trendlab.approvals.models import ApprovalScope
from trendlab.config.schema import AppConfig, PermissionMode
from trendlab.context.ignore import IgnoreRules
from trendlab.context.validation import detect_validation_commands, validation_commands
from trendlab.permissions.engine import PermissionEngine
from trendlab.permissions.models import Decision
from trendlab.permissions.rules import ProjectRules
from trendlab.providers.base import ToolCall
from trendlab.tools.base import ToolContext
from trendlab.tools.registry import default_registry
from trendlab.tools.runtime import ToolRuntime
from trendlab.ui.diff_view import diff_stats, unified_diff


def _rt(project, mgr, events, mode=PermissionMode.AUTO_EDIT):
    engine = PermissionEngine(mode)
    engine.project_rules = ProjectRules(project)
    ctx = ToolContext(
        project_root=project,
        session_id=mgr.session_id,
        ignore_rules=IgnoreRules.for_project(project),
        validation_commands={"test": "python3 -c 'print(1)'", "lint": "false"},
    )
    return ToolRuntime(default_registry(), engine, mgr, events, ctx), engine


def test_ignore_rules(project: Path):
    (project / ".gitignore").write_text("*.log\nbuild/\n!keep.log\n/rooted.txt\n")
    (project / ".trendlabignore").write_text("secret_dir/\n")
    rules = IgnoreRules.for_project(project)
    assert rules.ignored("x.log") and not rules.ignored("keep.log")
    assert rules.ignored("build", is_dir=True) and rules.ignored("build/out.o")
    assert rules.ignored("deep/build/x.py") and rules.ignored("node_modules/a/b.js")
    assert rules.ignored("rooted.txt") and not rules.ignored("sub/rooted.txt")
    assert rules.ignored("secret_dir/k.txt") and not rules.ignored("src/app.py")
    assert rules.ignored(".trendlab/sessions.db") and rules.ignored("img.png")
    (project / "build").mkdir()
    (project / "build" / "o.txt").write_text("x")
    (project / "a.log").write_text("x")
    files = [p.relative_to(project).as_posix() for p in rules.walk(project)]
    assert "src/app.py" in files and "a.log" not in files and "build/o.txt" not in files


def test_detect_validation(project: Path):
    (project / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length=100\n[tool.pytest.ini_options]\n"
    )
    (project / "tests").mkdir()
    cmds = detect_validation_commands(project)
    assert cmds["test"].endswith("-m pytest -q") and cmds["lint"] == "ruff check ."
    cfg = AppConfig(project={"test_command": "make check"})
    assert validation_commands(cfg, project)["test"] == "make check"
    (project / "package.json").write_text('{"scripts": {"test": "jest", "build": "tsc"}}')
    cmds = detect_validation_commands(project)
    assert cmds["build"] == "npm run build" and cmds["test"].endswith("-m pytest -q")


def test_unified_diff_and_stats():
    d = unified_diff("a.py", "x = 1\ny = 2\n", "x = 1\ny = 3\nz = 4\n")
    assert d.startswith("--- a/a.py") and diff_stats(d) == (2, 1)


async def test_glob_and_search_respect_ignores(project, manager_factory, events):
    rt, _ = _rt(project, manager_factory(), events)
    (project / "node_modules").mkdir()
    (project / "node_modules" / "x.py").write_text("TIMEOUT")
    (project / "src" / "b.py").write_text("def TIMEOUT_helper(): pass\n")
    res = await rt.execute(ToolCall(id="1", name="glob", arguments={"pattern": "**/*.py"}))
    assert "src/app.py" in res.output and "node_modules" not in res.output
    res = await rt.execute(ToolCall(id="2", name="search_text", arguments={"pattern": "TIMEOUT"}))
    assert (
        "src/app.py:1" in res.output
        and "src/b.py:1" in res.output
        and "node_modules" not in res.output
    )
    res = await rt.execute(ToolCall(id="3", name="list_directory", arguments={"path": "."}))
    assert "src/" in res.output and "node_modules/" not in res.output


async def test_read_file_binary_and_range(project, manager_factory, events):
    rt, _ = _rt(project, manager_factory(), events)
    (project / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    res = await rt.execute(ToolCall(id="1", name="read_file", arguments={"path": "blob.bin"}))
    assert not res.ok and "binary" in res.output
    (project / "big.txt").write_text("\n".join(f"line {i}" for i in range(1000)))
    res = await rt.execute(
        ToolCall(
            id="2",
            name="read_file",
            arguments={"path": "big.txt", "start_line": 10, "end_line": 12},
        )
    )
    assert res.output.startswith("10\tline 9") and "more lines" in res.output and res.data["sha256"]


async def test_patch_file_with_preview_and_conflicts(project, manager_factory, events, recorder):
    rt, _ = _rt(project, manager_factory(), events, mode=PermissionMode.ASK)
    tool = rt.registry.get("patch_file")
    args = tool.parse(
        {
            "path": "src/app.py",
            "old_text": "TIMEOUT = 30",
            "new_text": "TIMEOUT = 60",
            "explanation": "requested",
        }
    )
    perm = tool.permission(args, rt.ctx)
    assert perm.summary == "Patch src/app.py (+1 -1)" and "+TIMEOUT = 60" in perm.preview
    assert perm.explanation == "requested" and perm.affected_files == ["src/app.py"]
    rt.engine.mode = PermissionMode.AUTO_EDIT
    res = await rt.execute(ToolCall(id="1", name="patch_file", arguments=args.model_dump()))
    assert res.ok and (project / "src" / "app.py").read_text() == "TIMEOUT = 60\n"
    assert "+TIMEOUT = 60" in res.output and rt.changed_files["src/app.py"]
    res = await rt.execute(
        ToolCall(
            id="2",
            name="patch_file",
            arguments={"path": "src/app.py", "old_text": "nope", "new_text": "x"},
        )
    )
    assert not res.ok and "not found" in res.output
    (project / "src" / "app.py").write_text("a\na\n")
    res = await rt.execute(
        ToolCall(
            id="3",
            name="patch_file",
            arguments={"path": "src/app.py", "old_text": "a", "new_text": "b"},
        )
    )
    assert not res.ok and "occurs 2 times" in res.output
    res = await rt.execute(
        ToolCall(
            id="4",
            name="patch_file",
            arguments={"path": "src/app.py", "old_text": "a", "new_text": "b", "replace_all": True},
        )
    )
    assert res.ok and (project / "src" / "app.py").read_text() == "b\nb\n"
    res = await rt.execute(
        ToolCall(
            id="5",
            name="patch_file",
            arguments={
                "path": "src/app.py",
                "old_text": "b",
                "new_text": "c",
                "expected_sha256": "bad",
            },
        )
    )
    assert not res.ok and "conflict" in res.output
    assert recorder.of_type(
        __import__("trendlab.telemetry.events", fromlist=["EventType"]).EventType.FILE_CHANGED
    )


async def test_write_file_preview_for_new_and_existing(project, manager_factory, events):
    rt, _ = _rt(project, manager_factory(), events)
    tool = rt.registry.get("write_file")
    perm = tool.permission(tool.parse({"path": "new.py", "content": "x = 1\n"}), rt.ctx)
    assert perm.summary == "Create new.py (+1 -0)" and "+x = 1" in perm.preview
    perm = tool.permission(tool.parse({"path": "src/app.py", "content": "TIMEOUT = 99\n"}), rt.ctx)
    assert perm.summary == "Edit src/app.py (+1 -1)"


async def test_git_tools(project, manager_factory, events):
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "."], cwd=project, check=True
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
        cwd=project,
        check=True,
    )
    (project / "src" / "app.py").write_text("TIMEOUT = 31\n")
    rt, _ = _rt(project, manager_factory(), events)
    status = await rt.execute(ToolCall(id="1", name="git_status", arguments={}))
    assert status.ok and " M src/app.py" in status.output
    diff = await rt.execute(ToolCall(id="2", name="git_diff", arguments={"path": "src/app.py"}))
    assert "+TIMEOUT = 31" in diff.output
    log = await rt.execute(ToolCall(id="3", name="git_log", arguments={"max_count": 5}))
    assert "init" in log.output


async def test_run_tests_tool_records_validation(project, manager_factory, events):
    rt, _ = _rt(project, manager_factory(), events)
    res = await rt.execute(ToolCall(id="1", name="run_tests", arguments={"kind": "test"}))
    assert res.ok and res.data["validation_kind"] == "test"
    res = await rt.execute(ToolCall(id="2", name="run_tests", arguments={"kind": "lint"}))
    assert not res.ok and res.data["exit_code"] == 1
    res = await rt.execute(ToolCall(id="3", name="run_tests", arguments={"kind": "build"}))
    assert not res.ok and "no build command" in res.output
    assert [v["ok"] for v in rt.validation_runs] == [True, False]


async def test_malformed_tool_calls_get_corrective_feedback(project, manager_factory, events):
    rt, _ = _rt(project, manager_factory(), events)
    res = await rt.execute(
        ToolCall(id="1", name="_malformed", arguments={"error": "unknown tool 'x'"})
    )
    assert not res.ok and "MALFORMED TOOL CALL" in res.output and "read_file" in res.output
    res = await rt.execute(
        ToolCall(id="2", name="read_file", arguments={"_malformed_json": "{bad"})
    )
    assert not res.ok and "not valid JSON" in res.output
    res = await rt.execute(ToolCall(id="3", name="nope", arguments={}))
    assert "unknown tool" in res.output


async def test_project_scope_persists_rule(project, manager_factory, events):
    import asyncio

    mgr = manager_factory()
    rt, engine = _rt(project, mgr, events, mode=PermissionMode.ASK)
    task = asyncio.create_task(
        rt.execute(ToolCall(id="1", name="shell", arguments={"command": "touch p.txt"}))
    )
    for _ in range(50):
        await asyncio.sleep(0.01)
        if mgr.pending():
            break
    [req] = mgr.pending()
    # Remote clients may not grant project scope.
    from trendlab.approvals.models import ApprovalError

    with pytest.raises(ApprovalError) as exc:
        mgr.decide(
            req.approval_id,
            "approve",
            ApprovalScope.PROJECT,
            via="web",
            decision_token=req.decision_token,
            fingerprint=req.fingerprint,
        )
    assert exc.value.code == "scope_not_allowed"
    mgr.decide(req.approval_id, "approve", ApprovalScope.PROJECT, via="local", trusted=True)
    assert (await task).ok
    assert (project / ".trendlab" / "permissions.toml").read_text().strip().startswith("[rules]")
    # A fresh engine for the same project picks the rule up without asking.
    rt2, engine2 = _rt(project, mgr, events, mode=PermissionMode.ASK)
    # Rule keys use the command head ("touch p.txt"), so the same head is allowed, another is not.
    res = await rt2.execute(
        ToolCall(id="2", name="shell", arguments={"command": "touch p.txt q.txt"})
    )
    assert res.ok and (project / "q.txt").exists()
    shell = rt2.registry.get("shell")
    assert engine2.evaluate(
        shell.permission(shell.parse({"command": "touch p.txt z"}), rt2.ctx)
    ).matched_rule
    assert (
        engine2.evaluate(shell.permission(shell.parse({"command": "rm p.txt"}), rt2.ctx)).decision
        == Decision.ASK
    )
    assert ProjectRules(project).all() == {"shell:shell_write:touch p.txt": Decision.ALLOW}
