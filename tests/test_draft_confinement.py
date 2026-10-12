"""IL-B second review (evals D-115): every drafting-agent tool that executes anything runs in the
candidate-test sandbox profile: bubblewrap forced on, no network, a scrubbed environment, the
worker's secrets, the engine socket and /run/user hidden, git hardened."""

import asyncio
import contextlib
import shutil
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from trendlab.config.schema import AppConfig
from trendlab.engine.improve_policy import draft_config, forced_sandbox, secret_masks
from trendlab.security.sandbox import GIT_HARDEN_ARGS, GIT_HARDEN_ENV
from trendlab.tools.base import ToolContext
from trendlab.tools.git import GitDiffInput, GitDiffTool, GitStatusInput, GitStatusTool
from trendlab.tools.shell import ShellInput, ShellTool

HAS_BWRAP = shutil.which("bwrap") is not None
needs_bwrap = pytest.mark.skipif(not HAS_BWRAP, reason="needs bwrap")


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def shell(sb, root: Path, command: str, timeout: int = 30):
    ctx = ToolContext(project_root=root, session_id="s", sandbox=sb)
    return asyncio.run(ShellTool().run(ShellInput(command=command, timeout=timeout), ctx))


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=root)
    (root / "a.py").write_text("A = 1\n")
    sh("git", "add", "-A", cwd=root)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-q", "-m", "a",
       cwd=root)  # fmt: skip
    return root


@pytest.fixture
def outside_home(monkeypatch):
    """A worker home outside /tmp (the sandbox's private /tmp would hide a /tmp one anyway):
    a TrendLab home with a secret and a live engine socket."""
    base = Path(tempfile.mkdtemp(prefix="ilb-conf-", dir=Path.home()))
    th = base / "th"
    (th / "secrets").mkdir(parents=True)
    (th / "engine").mkdir()
    secret = th / "secrets" / "DEEPSEEK_API_KEY"
    secret.write_text("sk-fake-secret\n")
    sock_path = th / "engine" / "engine.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)
    monkeypatch.setenv("TRENDLAB_HOME", str(th))
    try:
        yield {"home": th, "secret": secret, "sock": sock_path}
    finally:
        server.close()
        for f in (sock_path, secret):
            with contextlib.suppress(OSError):
                f.unlink()  # exact files, then the now-empty folders
        for d in (th / "engine", th / "secrets", th, base):
            with contextlib.suppress(OSError):
                d.rmdir()


# -- the profile ---------------------------------------------------------------------------------
def test_the_profile_scrubs_env_cuts_network_and_masks(tmp_path, monkeypatch):
    monkeypatch.setattr("trendlab.security.sandbox.shutil.which", lambda n: f"/usr/bin/{n}")
    sb = forced_sandbox(tmp_path)
    argv = sb.wrap("true", cwd=tmp_path, allow_network=True)  # a network command asks for it
    assert argv[:2] == ["/usr/bin/env", "-i"]  # bwrap itself starts with no inherited env
    assert sb.bwrap in argv and "--unshare-net" in argv
    env = dict(a.split("=", 1) for a in argv[2 : argv.index(sb.bwrap)])
    assert set(env) <= {"PATH", "HOME", "LANG", *GIT_HARDEN_ENV, "GIT_CONFIG_COUNT"} | {
        f"GIT_CONFIG_{k}_{i}" for k in ("KEY", "VALUE") for i in range(2)
    }
    assert env["HOME"].startswith("/tmp/")
    masks = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
    assert "/run" in masks


def test_masks_cover_secrets_engine_runtime_and_ssh(tmp_path, monkeypatch):
    th = tmp_path / "th"
    (th / "engine").mkdir(parents=True)
    rt = tmp_path / "runtime"
    rt.mkdir()
    monkeypatch.setenv("TRENDLAB_HOME", str(th))
    monkeypatch.setenv("TRENDLAB_RUNTIME_DIR", str(rt))
    masks = secret_masks()
    assert th.resolve() in masks and rt.resolve() in masks
    for p in (Path.home() / ".ssh", Path.home() / ".trendlab", Path("/run/user")):
        if p.exists():
            assert p.resolve() in masks


def test_draft_config_denies_network_tools():
    cfg = draft_config(AppConfig(), 0.5, 60)
    assert {"web_fetch", "web_search", "delegate"} <= set(cfg.tools.deny)


# -- real sandbox runs -------------------------------------------------------------------------
@needs_bwrap
def test_tool_env_has_no_keys(repo, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-fake")
    monkeypatch.setenv("TRENDLAB_TELEGRAM_BOT_TOKEN", "123:fake")
    res = shell(forced_sandbox(repo), repo, "env; cat /proc/1/environ | tr '\\0' '\\n'")
    assert res.ok, res.output
    for leak in ("sk-ant-fake", "sk-ds-fake", "123:fake", "TRENDLAB_"):
        assert leak not in res.output
    assert "HOME=/tmp/" in res.output


@needs_bwrap
def test_secret_files_and_engine_socket_are_unreachable(repo, outside_home):
    secret, sock = outside_home["secret"], outside_home["sock"]
    probe = (
        "import socket, sys\n"
        "s = socket.socket(socket.AF_UNIX)\n"
        f"try:\n    s.connect({str(sock)!r})\nexcept OSError:\n    print('SOCK-BLOCKED')\n"
        "else:\n    print('SOCK-OPEN')\n"
    )
    (repo / "probe.py").write_text(probe)
    res = shell(
        forced_sandbox(repo),
        repo,
        f"cat {secret} 2>/dev/null || echo SECRET-HIDDEN; ls /run/user | wc -l; "
        f"cat ~/.ssh/* 2>/dev/null | wc -c; python3 probe.py",
    )
    assert "sk-fake-secret" not in res.output and "SECRET-HIDDEN" in res.output
    assert "SOCK-BLOCKED" in res.output and "SOCK-OPEN" not in res.output
    lines = res.output.split()
    assert lines[1] == "0" and lines[2] == "0"  # /run/user empty, no ~/.ssh file readable


@needs_bwrap
def test_the_candidate_test_sandbox_cannot_reach_the_engine_socket(repo, outside_home):
    sock = outside_home["sock"]
    sb = forced_sandbox(repo)
    cmd = (
        'python3 -c "import socket; s=socket.socket(socket.AF_UNIX); '
        f"s.connect('{sock}')\" && echo SOCK-OPEN || echo SOCK-BLOCKED"
    )
    out = subprocess.run(sb.wrap(cmd, cwd=repo), capture_output=True, text=True, timeout=30)
    assert "SOCK-BLOCKED" in out.stdout and "SOCK-OPEN" not in out.stdout


@needs_bwrap
def test_network_is_unavailable_even_for_network_commands(repo):
    probe = (
        "import socket\ns = socket.socket()\ns.settimeout(3)\n"
        "try:\n    s.connect(('1.1.1.1', 443))\nexcept OSError:\n    print('NET-BLOCKED')\n"
        "else:\n    print('NET-OPEN')\n"
    )
    (repo / "net.py").write_text(probe)
    # "pip" makes the shell tool ask for the network; the confined profile never grants it
    res = shell(forced_sandbox(repo), repo, "python3 net.py; pip --version >/dev/null 2>&1; true")
    assert "NET-BLOCKED" in res.output and "NET-OPEN" not in res.output


# -- git tools ---------------------------------------------------------------------------------
@needs_bwrap
def test_planted_fsmonitor_and_hook_never_run_through_git_tools(repo, tmp_path):
    inside = repo / "pwned-inside"
    outside = Path.home() / f".ilb-git-pwned-{uuid.uuid4().hex}"
    cmd = f"touch {inside} {outside}"
    sh("git", "config", "core.fsmonitor", f"{cmd}; false", cwd=repo)
    for hook in ("post-index-change", "pre-commit", "post-checkout"):
        p = repo / ".git" / "hooks" / hook
        p.write_text(f"#!/bin/sh\n{cmd}\n")
        p.chmod(0o755)
    (repo / "a.py").write_text("A = 2\n")  # a dirty tree makes status refresh the index
    ctx = ToolContext(project_root=repo, session_id="s", sandbox=forced_sandbox(repo))
    try:
        st = asyncio.run(GitStatusTool().run(GitStatusInput(), ctx))
        diff = asyncio.run(GitDiffTool().run(GitDiffInput(), ctx))
        assert st.ok and "a.py" in st.output, st.output
        assert diff.ok and "A = 2" in diff.output
        assert not inside.exists() and not outside.exists()
    finally:
        with contextlib.suppress(OSError):
            outside.unlink()


def test_git_tools_run_inside_the_sandbox_with_hardening(repo, monkeypatch):
    monkeypatch.setattr("trendlab.security.sandbox.shutil.which", lambda n: f"/usr/bin/{n}")
    seen = []

    async def fake_exec(*argv, **kw):
        seen.append(list(argv))
        raise OSError("stop here")

    monkeypatch.setattr("trendlab.tools.git.asyncio.create_subprocess_exec", fake_exec)
    ctx = ToolContext(project_root=repo, session_id="s", sandbox=forced_sandbox(repo))
    res = asyncio.run(GitStatusTool().run(GitStatusInput(), ctx))
    assert not res.ok
    argv = seen[0]
    assert argv[:2] == ["/usr/bin/env", "-i"] and "/usr/bin/bwrap" in argv
    assert "--unshare-net" in argv
    script = argv[-1]
    for part in (*GIT_HARDEN_ARGS, *(f"{k}={v}" for k, v in GIT_HARDEN_ENV.items())):
        assert part in script


def test_git_tools_fail_closed_without_bwrap(repo, monkeypatch):
    monkeypatch.setattr("trendlab.security.sandbox.shutil.which", lambda n: None)
    ctx = ToolContext(project_root=repo, session_id="s", sandbox=forced_sandbox(repo))
    res = asyncio.run(GitStatusTool().run(GitStatusInput(), ctx))
    assert not res.ok and "sandbox" in res.output


def test_the_drafting_app_uses_the_confined_sandbox(tmp_path, monkeypatch):
    from trendlab.app import TrendLabApp
    from trendlab.engine import candidate_runner as cr

    seen = {}

    class FakeApp:
        def __init__(self, path, cfg, console=None):
            self.sandbox_factory = None
            self.checkpoint_head = None
            seen["app"] = self

        async def start(self, interactive=False):
            seen["sandbox"] = self.sandbox_factory(tmp_path)
            self.session_id, self.costs, self.gateway = "s", None, object()

        async def run_prompt(self, prompt):
            return None

        async def stop(self):
            return None

    class FakeUnattended:
        def __init__(self, tl):
            pass

        def close(self):
            pass

    monkeypatch.setattr("trendlab.app.TrendLabApp", FakeApp)
    monkeypatch.setattr("trendlab.benchmarks.unattended.Unattended", FakeUnattended)
    cfg = AppConfig()
    cfg.improvement.prod_dir = str(tmp_path / "prod")
    (tmp_path / "dev" / ".git").mkdir(parents=True)
    cfg.engine.harness_root = str(tmp_path / "dev")
    hooks = cr.AgentHooks(started=lambda sid, costs, gateway=None: None, base_sha="a" * 40)
    asyncio.run(cr.default_run_agent(tmp_path, cfg, "p", hooks))
    sb = seen["sandbox"]
    assert seen["app"].checkpoint_head == "a" * 40  # the checkpoint never asks git (residual 2)
    (tmp_path / "dev" / ".git" / "objects").mkdir()
    sb = seen["app"].sandbox_factory(tmp_path)
    assert (tmp_path / "dev" / ".git" / "objects") in sb.ro_paths
    assert sb.confined and sb.mode == "on" and sb.config.allow_network is False
    assert str(tmp_path / "prod" / ".venv" / "bin") in sb.env["PATH"]
    assert hasattr(TrendLabApp, "_make_sandbox")


# -- fix round 2: allowlist profile, /run hidden, masks applied last -----------------------------
def prod_checkout(tmp_path: Path) -> Path:
    """A production checkout whose venv is the one running this suite (outside the checkout)."""
    import sys

    prod = tmp_path / "prod"
    prod.mkdir()
    (prod / "evil.py").write_text("EVIL = True\n")
    (prod / ".venv").symlink_to(sys.prefix)
    return prod


def both_sandboxes(repo: Path, prod: Path):
    """The drafting agent's tool sandbox and the candidate-test sandbox, as production builds them."""
    from trendlab.engine.candidate_runner import test_sandbox

    tool = forced_sandbox(repo, ro_paths=[prod], prod_dir=prod)
    return {"tool": tool, "tests": test_sandbox(prod, repo)}


def run_in(sb, repo: Path, command: str) -> str:
    out = subprocess.run(sb.wrap(command, cwd=repo), capture_output=True, text=True, timeout=60)
    return out.stdout + out.stderr


def test_the_profile_hides_all_of_run(tmp_path, monkeypatch):
    monkeypatch.setattr("trendlab.security.sandbox.shutil.which", lambda n: f"/usr/bin/{n}")
    argv = forced_sandbox(tmp_path).wrap("true", cwd=tmp_path)
    assert ["--tmpfs", "/run"] == argv[argv.index("/run") - 1 : argv.index("/run") + 1]
    assert ["--remount-ro", "/run"] == argv[len(argv) - argv[::-1].index("/run") - 2 :][:2]
    # nothing under /run is bound back, and / itself is never bound
    pairs = [(argv[i], argv[i + 2]) for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")]
    assert not [d for _, d in pairs if d == "/run" or d.startswith("/run/")]
    assert ("--ro-bind", "/") not in pairs and "/" not in [d for _, d in pairs]


@needs_bwrap
def test_no_unix_socket_is_visible_in_either_sandbox(repo, tmp_path, outside_home):
    prod = prod_checkout(tmp_path)
    scan = (
        "ls -A /run | wc -l; "
        "test -e /run/trendlab-bridge && echo BRIDGE-VISIBLE || echo BRIDGE-HIDDEN; "
        "find / \\( -path /proc -o -path /sys -o -path /usr \\) -prune -o -type s -print "
        "2>/dev/null; echo SCAN-DONE"
    )
    for name, sb in both_sandboxes(repo, prod).items():
        out = run_in(sb, repo, scan)
        lines = out.split()
        assert lines[0] == "0", (name, out)  # /run is empty: no tmux bridge, no WSL interop
        assert "BRIDGE-HIDDEN" in out and "BRIDGE-VISIBLE" not in out, (name, out)
        assert "SCAN-DONE" in out, (name, out)
        sockets = [x for x in lines[2:] if x.startswith("/") and x != "SCAN-DONE"]
        assert sockets == [], (name, sockets)  # not the engine socket, not anything


@needs_bwrap
def test_an_arbitrary_file_in_the_worker_home_is_unreadable(repo, tmp_path):
    prod = prod_checkout(tmp_path)
    base = Path(tempfile.mkdtemp(prefix="ilb-home-", dir=Path.home()))
    cred = base / "credentials"
    cred.write_text("hunter2-token\n")
    try:
        for name, sb in both_sandboxes(repo, prod).items():
            out = run_in(
                sb,
                repo,
                f"cat {cred} 2>/dev/null || echo CRED-HIDDEN; ls -A {Path.home()} | wc -l; "
                "ls -A /; python3 -c 'print(\"PY-OK\")'; "
                f"cat {prod / 'evil.py'}; touch {prod / 'w'} 2>/dev/null || echo PROD-RO; "
                "touch ok-rw && echo WT-RW",
            )
            assert "hunter2" not in out and "CRED-HIDDEN" in out, (name, out)
            assert "PY-OK" in out and "EVIL = True" in out, (name, out)
            assert "PROD-RO" in out and "WT-RW" in out, (name, out)
            top = set(out.split()) & {"srv", "var", "mnt", "opt", "media", "snap", "boot"}
            assert not top, (name, out)  # an allowlist: only system dirs and the binds
            (repo / "ok-rw").unlink()
    finally:
        cred.unlink()
        base.rmdir()


def test_a_mask_inside_a_bind_is_applied_after_it(tmp_path, monkeypatch):
    monkeypatch.setattr("trendlab.security.sandbox.shutil.which", lambda n: f"/usr/bin/{n}")
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    sb = forced_sandbox(tmp_path)
    sb.masks = [secret_dir]
    argv = sb.wrap("true", cwd=tmp_path)
    bind_at = max(i for i, a in enumerate(argv) if a == "--bind" and argv[i + 2] == str(tmp_path))
    mask_at = max(
        i for i, a in enumerate(argv) if a == "--tmpfs" and argv[i + 1] == str(secret_dir)
    )
    assert mask_at > bind_at  # a later bind would mount the folder back over the mask


def test_a_mask_that_contains_a_bind_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("trendlab.security.sandbox.shutil.which", lambda n: f"/usr/bin/{n}")
    wt = tmp_path / "outer" / "wt"
    wt.mkdir(parents=True)
    sb = forced_sandbox(wt)
    sb.masks = [tmp_path / "outer"]
    with pytest.raises(RuntimeError, match="hidden path contains"):
        sb.wrap("true", cwd=wt)


@needs_bwrap
def test_a_masked_folder_inside_the_bound_project_stays_hidden(repo):
    # the reviewer's case: the project is bound, a folder inside it is masked
    (repo / "secret").mkdir()
    (repo / "secret" / "key").write_text("top-secret-key\n")
    (repo / "token.txt").write_text("top-secret-token\n")
    sb = forced_sandbox(repo)
    sb.masks = [repo / "secret", repo / "token.txt"]
    res = shell(sb, repo, "cat secret/key token.txt 2>&1; ls -A secret | wc -l; echo DONE")
    assert "top-secret" not in res.output and "DONE" in res.output, res.output


@needs_bwrap
def test_git_works_in_a_worktree_with_the_dev_clone_git_dir_read_only(tmp_path):
    dev = tmp_path / "dev"
    dev.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=dev)
    (dev / "a.py").write_text("A = 1\n")
    sh("git", "add", "-A", cwd=dev)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-q", "-m", "a",
       cwd=dev)  # fmt: skip
    sh("git", "config", "remote.origin.url", "https://user:ghp_secret@example.invalid/r.git",
       cwd=dev)  # fmt: skip
    wt = dev / ".trendlab" / "worktrees" / "cand-x-1"
    sh("git", "worktree", "add", "-q", "-b", "improve/cand-x-1", str(wt), cwd=dev)
    (wt / "a.py").write_text("A = 2\n")
    sb = forced_sandbox(wt, git_dir=dev / ".git")
    ctx = ToolContext(project_root=wt, session_id="s", sandbox=sb)
    st = asyncio.run(GitStatusTool().run(GitStatusInput(), ctx))
    assert st.ok and "a.py" in st.output, st.output
    res = shell(sb, wt, f"cat {dev / '.git' / 'config'}; echo DONE")
    assert "ghp_secret" not in res.output and "DONE" in res.output


# -- I-5: no process-spawning tool outside the sandbox's reach ------------------------------------
def test_draft_config_denies_background_processes():
    cfg = draft_config(AppConfig(), 0.5, 60)
    assert "background_process" in cfg.tools.deny


# -- residual 2: the checkpoint never asks git ---------------------------------------------------
def test_the_checkpoint_uses_the_draft_base_sha_not_git(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from trendlab.app import TrendLabApp

    async def no_git(root):
        raise AssertionError("git must not run on a confined draft's worktree")

    monkeypatch.setattr("trendlab.tools.git.git_head", no_git)
    made = {}

    class Checkpoints:
        def create(self, files, label, git_head=None):
            made["head"] = git_head
            return {"id": "c1"}

    fake = SimpleNamespace(
        checkpoints=Checkpoints(),
        plan=None,
        _run_checkpoint=None,
        checkpoint_head="b" * 40,
        project_root=tmp_path,
        session_id="s",
        events=SimpleNamespace(emit=lambda *a, **k: None),
    )
    asyncio.run(TrendLabApp._checkpoint_before_mutation(fake, ["a.py"]))
    assert made["head"] == "b" * 40
