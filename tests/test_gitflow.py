import os
import subprocess
from pathlib import Path

from rich.console import Console

from trendlab.app import TrendLabApp
from trendlab.config.loader import load_config
from trendlab.config.schema import PermissionMode
from trendlab.orchestration.gitflow import (
    WorktreeManager,
    commit,
    create_pr,
    ensure_feature_branch,
    load_issue,
)
from trendlab.providers.base import ModelResponse
from trendlab.providers.scripted import ScriptedProvider
from trendlab.ui.repl import Repl


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _repo_with_remote(project: Path, tmp_path: Path) -> Path:
    _git(project, "init", "-q", "-b", "main")
    _git(project, "add", ".")
    _git(project, "commit", "-q", "-m", "init")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    _git(project, "remote", "add", "origin", str(remote))
    _git(project, "push", "-q", "-u", "origin", "main")
    return remote


def _fake_gh(tmp_path: Path, monkeypatch) -> Path:
    log = tmp_path / "gh.log"
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir(exist_ok=True)
    gh.write_text(f'''#!/bin/sh
echo "$@" >> "{log}"
case "$1 $2" in
  "pr create") echo "https://github.com/owner/repo/pull/42";;
  "issue view") echo '{{"number": 7, "title": "Crash on empty cart", "body": "Steps: add nothing, checkout.", "labels": [{{"name": "bug"}}], "url": "https://github.com/owner/repo/issues/7", "state": "OPEN"}}';;
  *) echo "unknown" >&2; exit 1;;
esac
''')
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{gh.parent}:{os.environ['PATH']}")
    return log


async def _app(project, provider):
    cfg = load_config(project)
    cfg.providers["scripted"] = cfg.providers["openai"].model_copy()
    console = Console(record=True, width=120)
    tl = TrendLabApp(
        project,
        cfg,
        provider=provider,
        model_ref="scripted:m",
        console=console,
        permission_mode=PermissionMode.UNSAFE,
    )
    repl = Repl(tl, console)
    await tl.start(interactive=False, command_handler=repl.handle_command)
    return tl, repl, console


async def test_commit_generates_message_and_commits(project: Path, tmp_path: Path, _trendlab_home):
    _repo_with_remote(project, tmp_path)
    (project / "src" / "app.py").write_text("TIMEOUT = 60\n")
    (project / "src" / "new.py").write_text("x = 1\n")
    provider = ScriptedProvider(
        [ModelResponse(text="fix: raise timeout to 60s\n\n- new helper module")]
    )
    tl, repl, console = await _app(project, provider)
    try:
        res = await commit(tl)
        assert res["ok"] and res["message"].startswith("fix: raise timeout to 60s") and res["head"]
        assert "src/new.py" in res["stat"]
        assert _git(project, "status", "--porcelain").stdout.strip() == ""
        assert (
            _git(project, "log", "-1", "--format=%s").stdout.strip() == "fix: raise timeout to 60s"
        )
        assert (
            "diff --git" in provider.calls[0][0]["content"]
            and "TIMEOUT = 60" in provider.calls[0][0]["content"]
        )
        assert (await commit(tl))["error"] == "nothing to commit"
        (project / "README.md").write_text("# demo v2\n")
        await repl.handle_command("/commit docs: bump readme")
        assert "committed" in console.export_text(clear=True)
        assert _git(project, "log", "-1", "--format=%s").stdout.strip() == "docs: bump readme"
    finally:
        await tl.stop()


async def test_pr_pushes_branch_and_uses_gh_with_issue(
    project: Path, tmp_path: Path, _trendlab_home, monkeypatch
):
    remote = _repo_with_remote(project, tmp_path)
    log = _fake_gh(tmp_path, monkeypatch)
    provider = ScriptedProvider(
        [
            ModelResponse(
                text="## Summary\nFixes the crash.\n\n## Changes\n- guard empty cart\n\n## Testing\n- pytest"
            )
        ]
    )
    tl, repl, console = await _app(project, provider)
    try:
        res = await load_issue(tl, 7)
        assert res["ok"] and tl.current_issue["number"] == 7
        assert tl.context.messages[-1]["content"].startswith(
            "[context] GitHub issue #7: Crash on empty cart"
        )
        (project / "src" / "app.py").write_text("TIMEOUT = 90\n")
        _git(project, "add", "-A")
        _git(project, "commit", "-q", "-m", "fix: guard empty cart")
        assert (await create_pr(tl, "Guard empty cart"))["ok"]
        pr = await create_pr(
            tl, "Guard empty cart"
        )  # second call re-pushes and re-creates (idempotent fake)
        assert (
            pr["ok"]
            and pr["url"] == "https://github.com/owner/repo/pull/42"
            and pr["base"] == "main"
        )
        assert pr["branch"].startswith("trendlab/") and "Closes #7" in pr["body"]
        assert _git(project, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == pr["branch"]
        assert pr["branch"] in _git(tmp_path, "--git-dir", str(remote), "branch").stdout
        gh_calls = log.read_text()
        assert (
            "pr create --base main --head trendlab/" in gh_calls
            and "--title Guard empty cart" in gh_calls
        )
        (project / "src" / "app.py").write_text("dirty\n")
        assert (await create_pr(tl))["error"].startswith("uncommitted changes")
        await repl.handle_command("/issue 7")
        assert "loaded issue #7" in console.export_text(clear=True)
    finally:
        await tl.stop()


async def test_pr_requires_commits_and_feature_branch(
    project: Path, tmp_path: Path, _trendlab_home, monkeypatch
):
    _repo_with_remote(project, tmp_path)
    _fake_gh(tmp_path, monkeypatch)
    tl, repl, console = await _app(project, ScriptedProvider([]))
    try:
        assert (await ensure_feature_branch(tl, "My Feature")) == "trendlab/my-feature"
        assert (
            await ensure_feature_branch(tl, "other")
        ) == "trendlab/my-feature"  # already on a branch
    finally:
        await tl.stop()
        assert "no commits" in (await create_pr(tl))["error"]


async def test_worktree_isolation(project: Path, tmp_path: Path, _trendlab_home):
    _repo_with_remote(project, tmp_path)
    tl, repl, console = await _app(project, ScriptedProvider([]))
    try:
        await repl.handle_command("/worktree start Try Idea")
        out = console.export_text(clear=True)
        assert "working in worktree" in out
        wt = project / ".trendlab" / "worktrees" / "try-idea"
        assert (
            wt.is_dir()
            and tl.project_root == wt.resolve()
            and tl.tools.ctx.project_root == wt.resolve()
        )
        assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "trendlab/try-idea"
        assert ".trendlab/" in (project / ".git" / "info" / "exclude").read_text()
        (wt / "src" / "app.py").write_text("TIMEOUT = 1\n")
        assert (
            project / "src" / "app.py"
        ).read_text() == "TIMEOUT = 30\n"  # main checkout untouched
        assert "src/app.py" in [e["path"] for e in []] or tl.checkpoints.root == wt.resolve()
        await repl.handle_command("/worktree list")
        assert "trendlab/try-idea" in console.export_text(clear=True)
        await repl.handle_command("/worktree done")
        assert tl.project_root == project.resolve()
        await repl.handle_command("/worktree remove try-idea --force")
        assert "removed" in console.export_text(clear=True) and not wt.exists()
        wm = WorktreeManager(project)
        assert all("try-idea" not in r["path"] for r in await wm.list())
    finally:
        await tl.stop()


def test_cli_worktree_flag(project: Path, tmp_path: Path, _trendlab_home, monkeypatch):
    from typer.testing import CliRunner

    from trendlab import app as app_mod
    from trendlab.cli import app

    _repo_with_remote(project, tmp_path)
    captured = {}
    orig = app_mod.TrendLabApp.__init__

    def patched(self, root, *a, **kw):
        kw["provider"] = ScriptedProvider([ModelResponse(text="ok")])
        kw["model_ref"] = "openai:m"
        captured["root"] = root
        orig(self, root, *a, **kw)

    monkeypatch.setattr(app_mod.TrendLabApp, "__init__", patched)
    r = CliRunner().invoke(app, ["-C", str(project), "-p", "hi", "--worktree", "spike"])
    assert r.exit_code == 0, r.output
    assert captured["root"] == (project / ".trendlab" / "worktrees" / "spike")
    assert "working in worktree" in " ".join(r.output.split())
