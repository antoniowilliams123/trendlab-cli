"""Uplift U28: a checkpoint per plan step, and undo back to before any step."""

from pathlib import Path

from rich.console import Console

from trendlab.agent.planner import apply_steps
from trendlab.app import TrendLabApp
from trendlab.config.loader import load_config
from trendlab.config.schema import PermissionMode, RemoteApprovalConfig
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.sessions.checkpoints import undo_step


def _call(i, name, **args):
    return ModelResponse(tool_calls=[ToolCall(id=str(i), name=name, arguments=args)])


async def test_step_checkpoints_and_undo_by_step(project: Path, _trendlab_home: Path):
    cfg = load_config(project)
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    cfg.sessions.record_cassettes = False
    cfg.planner.design_checkpoint = False  # this test is about undo, not design
    provider = ScriptedProvider(
        [
            _call(1, "write_file", path="src/app.py", content="TIMEOUT = 60\n"),
            _call(2, "task", action="complete", task_id="T-1", evidence="timeout raised"),
            _call(3, "write_file", path="src/extra.py", content="X = 1\n"),
            _call(4, "write_file", path="src/app.py", content="TIMEOUT = 90\n"),
            _call(5, "task", action="complete", task_id="T-2", evidence="extra added"),
            ModelResponse(text="Both steps done."),
        ]
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
        apply_steps(
            tl.plan,
            [
                {
                    "title": "Raise the timeout",
                    "files": ["src/app.py"],
                    "done_when": "",
                    "validation": None,
                },
                {
                    "title": "Add extra",
                    "files": ["src/extra.py"],
                    "done_when": "",
                    "validation": None,
                },
            ],
        )
        await tl.run_prompt("raise the timeout, then add extra")
        labels = [c["label"] for c in tl.checkpoints.list()]
        assert labels == ["auto", "step T-1: Raise the timeout", "step T-2: Add extra"]
        res = undo_step(tl.checkpoints, "T-2")
        assert res["ok"] and res["removed"] == ["src/extra.py"]
        assert (project / "src/app.py").read_text() == "TIMEOUT = 60\n"  # back to end of T-1
        assert not (project / "src/extra.py").exists()
        back = undo_step(tl.checkpoints, "T-1")
        assert back["ok"] and (project / "src/app.py").read_text() == "TIMEOUT = 30\n"
        assert undo_step(tl.checkpoints, "T-9")["ok"] is False
    finally:
        await tl.stop()
