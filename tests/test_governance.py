"""Uplift U9: communication constraints, change-scope constraint, prompt-level commits."""

import subprocess
from pathlib import Path

from rich.console import Console

from trendlab.agent.scope import outside_scope
from trendlab.agent.style import check, measure, rules_text
from trendlab.app import TrendLabApp
from trendlab.config.loader import load_config
from trendlab.config.schema import GovernanceConfig, PermissionMode, RemoteApprovalConfig
from trendlab.providers.base import ModelResponse, ToolCall
from trendlab.providers.scripted import ScriptedProvider
from trendlab.sessions.turns import branch_for, commit_turn, list_turns
from trendlab.telemetry.events import EventType

LONG = (
    "Certainly! I hope this helps. The RPC layer now uses the new TTL cache, which I leveraged "
    "to seamlessly delve into the problem space and make the whole thing robust and scalable "
    "across every module in the repository so that nothing will ever break again in any way."
)


def test_measure_counts_prose_not_code():
    text = "Fixed the bug.\n\n```python\nx = 1\ny = 2\n```\n\nTests pass now."
    m = measure(text)
    assert m["words"] == 6 and m["sentences"] == 2 and m["walls_of_text"] == 0
    assert m["reading_ease"] > 60
    long = measure(LONG)
    assert {"certainly!", "i hope this helps", "delve", "leverage"} <= set(long["robospeak"])
    assert long["undefined_acronyms"] == ["RPC", "TTL"]
    defined = measure("The time to live (TTL) is 30s. The TTL resets on write.")
    assert defined["undefined_acronyms"] == []
    wall = measure(" ".join(["word"] * 130) + ".")
    assert wall["walls_of_text"] == 1
    bullets = measure("\n".join(f"- item {i} " + "word " * 10 for i in range(12)))
    assert bullets["walls_of_text"] == 0


def test_check_enforces_only_configured_rules():
    m = measure(LONG)
    assert check(m, GovernanceConfig()) == []  # nothing configured, nothing enforced
    issues = check(m, GovernanceConfig(max_answer_words=20, plain_language=True))
    assert any("limit is 20" in i for i in issues)
    assert any("spell out RPC, TTL" in i for i in issues)
    assert any("filler" in i for i in issues)
    assert rules_text(GovernanceConfig()) == ""
    text = rules_text(GovernanceConfig(max_answer_words=60, plain_language=True, tone="direct"))
    assert "at most 60 words" in text and "Plain language" in text and "Tone: direct" in text


def _app(project, cfg, responses):
    cfg.remote_approval = RemoteApprovalConfig(enabled=False)
    provider = ScriptedProvider(responses)
    return TrendLabApp(
        project,
        cfg,
        provider=provider,
        model_ref="scripted:m",
        permission_mode=PermissionMode.UNSAFE,
        console=Console(quiet=True),
    )


async def test_answer_over_the_limit_is_rewritten_once(project: Path, _trendlab_home: Path):
    cfg = load_config(project)
    cfg.governance.max_answer_words = 12
    tl = _app(
        project, cfg, [ModelResponse(text=LONG), ModelResponse(text="Checked: no change needed.")]
    )
    seen = []
    await tl.start(interactive=False)
    tl.events.subscribe(
        lambda e: seen.append(e.data) if e.type == EventType.COMMUNICATION_CHECKED else None
    )
    try:
        assert "at most 12 words" in tl.context.system_prompt  # stated before
        result = await tl.run_prompt("is the timeout right?")
    finally:
        await tl.stop()
    assert [bool(d["issues"]) for d in seen] == [True, False]  # checked after
    assert seen[0]["rewrite_requested"] is True
    assert result.status == "COMPLETED" and result.communication["words"] == 4


async def test_rewrite_is_requested_only_once(project: Path, _trendlab_home: Path):
    cfg = load_config(project)
    cfg.governance.max_answer_words = 12
    tl = _app(project, cfg, [ModelResponse(text=LONG), ModelResponse(text=LONG)])
    await tl.start(interactive=False)
    try:
        result = await tl.run_prompt("is the timeout right?")
    finally:
        await tl.stop()
    assert result.status == "COMPLETED" and result.communication["issues"]
    assert result.communication["nudged"] is True


async def test_change_scope_refuses_edits_outside_the_allowlist(
    project: Path, _trendlab_home: Path
):
    cfg = load_config(project)
    cfg.governance.change_allow = ["src/*"]

    def write(i, path):
        return ModelResponse(
            tool_calls=[
                ToolCall(
                    id=str(i), name="write_file", arguments={"path": path, "content": "x = 1\n"}
                )
            ]
        )

    tl = _app(
        project,
        cfg,
        [
            write(1, "README.md"),
            write(2, "src/app.py"),
            ModelResponse(text="Updated src/app.py only."),
        ],
    )
    skipped = []
    await tl.start(interactive=False)
    tl.events.subscribe(
        lambda e: skipped.append(e.data) if e.type == EventType.TOOL_SKIPPED else None
    )
    try:
        result = await tl.run_prompt("set x")
    finally:
        await tl.stop()
    assert (project / "README.md").read_text() == "# demo\n"
    assert (project / "src/app.py").read_text() == "x = 1\n"
    assert skipped and skipped[0]["reason"] == "outside_change_scope"
    assert result.changed_files == ["src/app.py"]
    assert outside_scope(["src/a/b.py", "docs/x.md"], ["src"], project) == ["docs/x.md"]
    assert outside_scope(["anything"], [], project) == []


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


async def test_turn_commits_land_on_a_side_branch_only(project: Path, _trendlab_home: Path):
    _git(project, "init", "-q")
    _git(project, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    head = _git(project, "rev-parse", "HEAD")
    cfg = load_config(project)
    cfg.governance.commit_per_turn = True
    write = ModelResponse(
        tool_calls=[
            ToolCall(
                id="w",
                name="write_file",
                arguments={"path": "src/app.py", "content": "TIMEOUT = 60\n"},
            )
        ]
    )
    tl = _app(
        project,
        cfg,
        [
            write,
            ModelResponse(text="Raised the timeout to 60."),
            ModelResponse(text="Nothing else to change."),
        ],
    )
    committed = []
    await tl.start(interactive=False)
    tl.events.subscribe(
        lambda e: committed.append(e.data) if e.type == EventType.TURN_COMMITTED else None
    )
    try:
        await tl.run_prompt("raise the timeout to 60")
        await tl.run_prompt("anything else?")  # no changes → no commit
        sid = tl.session_id
    finally:
        await tl.stop()
    assert len(committed) == 1 and committed[0]["turn"] == 1
    branch = branch_for(sid)
    assert _git(project, "rev-parse", "HEAD") == head  # the user's branch did not move
    assert _git(project, "status", "--porcelain") == "M src/app.py"  # index untouched
    msg = _git(project, "log", "-1", "--format=%B", branch)
    assert msg.startswith("turn 1: raise the timeout to 60") and "files: src/app.py" in msg
    assert "TIMEOUT = 60" in list_turns(project, sid, patch=True)

    # nothing changed since the last turn → no new commit
    class R:
        status, changed_files, validation_runs, stop_reason = "COMPLETED", ["src/app.py"], [], None

    assert commit_turn(project, sid, prompt="again", result=R(), turn=2) is None
    assert commit_turn(Path.home(), sid, prompt="x", result=R(), turn=1) is None  # never home
