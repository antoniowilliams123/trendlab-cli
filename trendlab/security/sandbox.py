"""OS-level sandbox for shell commands (spec §36, §75): bubblewrap when available.

Inside the sandbox the whole filesystem is read-only except the project directory, /tmp and the
configured writable paths; the network is cut unless allowed; the process dies with TrendLab.
This is what makes unsafe mode (no prompts) safe to leave running.
"""

from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from trendlab.config.schema import SandboxConfig

# Git run on a tree someone else controls (IL-B, evals D-115): nothing configured in that tree,
# the system or the user's global config may run a command.
GIT_HARDEN_ARGS = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null")
GIT_HARDEN_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
ENV_BIN = "/usr/bin/env"


def bwrap_position(argv: list[str], bwrap: str | None) -> int | None:
    """Where bubblewrap starts in ``argv``: 0, or right after ``env -i NAME=value...``; None
    when ``argv`` would not run under it."""
    if not bwrap or not argv:
        return None
    if argv[0] == bwrap:
        return 0
    if argv[:2] != [ENV_BIN, "-i"] or bwrap not in argv:
        return None
    i = argv.index(bwrap)
    return i if all("=" in a for a in argv[2:i]) else None


@dataclass
class Sandbox:
    config: SandboxConfig
    project_root: Path
    bwrap: str | None = field(default_factory=lambda: shutil.which("bwrap"))

    docker: str | None = field(default_factory=lambda: shutil.which("docker"))
    # IL-B (evals D-115): a caller that must never run unsandboxed pins the mode here, and the
    # TRENDLAB_SANDBOX environment variable is then ignored.
    force_mode: str | None = None
    # Extra read-only binds made after the private /tmp, so a path under /tmp stays visible.
    ro_paths: list[Path] = field(default_factory=list)
    # IL-B confined profile (evals D-115): the network is always cut, whatever the caller asks;
    # bubblewrap itself starts from ``env -i`` with only ``env``, so no secret is inherited (not
    # even through /proc/1/environ inside); every path in ``masks`` is hidden (a tmpfs over a
    # folder, /dev/null over a file).
    confined: bool = False
    env: dict[str, str] = field(default_factory=dict)
    masks: list[Path] = field(default_factory=list)

    @property
    def available(self) -> bool:
        if self.mode == "docker":
            return self.docker is not None
        return self.bwrap is not None

    @property
    def mode(self) -> str:
        """Effective mode: ``force_mode`` when set; otherwise the TRENDLAB_SANDBOX env var
        (off/on/auto) overrides the config."""
        if self.force_mode is not None:
            return self.force_mode
        env = os.environ.get("TRENDLAB_SANDBOX", "").strip().lower()
        return env if env in {"off", "on", "auto", "docker"} else self.config.mode

    @property
    def active(self) -> bool:
        if self.mode == "off":
            return False
        if self.mode == "on" and not self.available:
            raise RuntimeError("sandbox mode is 'on' but bubblewrap (bwrap) is not installed")
        if self.mode == "docker" and not self.available:
            raise RuntimeError("sandbox mode is 'docker' but docker is not installed")
        return self.available

    def writable_paths(self, extra: list[str] | None = None) -> list[Path]:
        paths = [self.project_root.resolve()]
        for raw in [*self.config.writable_paths, *(extra or [])]:
            p = Path(os.path.expandvars(raw)).expanduser()
            if p.exists():
                paths.append(p.resolve())
        return paths

    def wrap(self, command: str, *, cwd: Path, allow_network: bool | None = None) -> list[str]:
        """argv that runs ``command`` inside the sandbox (or plain sh when inactive)."""
        if not self.active:
            return ["/bin/sh", "-c", command]
        net = self.config.allow_network if allow_network is None else allow_network
        if self.confined:
            net = False
        if self.mode == "docker":
            return self._docker(command, cwd=cwd, net=net)
        assert self.bwrap
        argv: list[str] = []
        if self.confined:
            argv += [ENV_BIN, "-i", *(f"{k}={v}" for k, v in self.env.items())]
        argv += [
            self.bwrap,
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
        ]
        binds = [Path(cwd).resolve(), *(Path(p).resolve() for p in self.ro_paths)]
        binds += self.writable_paths()
        for m in self.masks:
            p = Path(m).resolve()
            if any(b == p or p in b.parents for b in binds):
                raise RuntimeError(f"a hidden path contains a path the sandbox must show: {p}")
            if p.is_dir():
                argv += ["--tmpfs", str(p), "--remount-ro", str(p)]  # hidden and unwritable
            elif p.exists():
                argv += ["--ro-bind", "/dev/null", str(p)]
        # Private /tmp first; binds after it so a project under /tmp is still mounted rw.
        argv += ["--tmpfs", "/tmp"]
        home = self.env.get("HOME", "") if self.confined else ""
        if home.startswith("/tmp/"):
            argv += ["--dir", home]
        for p in self.ro_paths:
            argv += ["--ro-bind", str(Path(p).resolve()), str(Path(p).resolve())]
        for p in self.writable_paths():
            argv += ["--bind", str(p), str(p)]
        argv += ["--unshare-user-try", "--unshare-pid", "--unshare-ipc", "--unshare-uts"]
        if not net:
            argv.append("--unshare-net")
        argv += ["--die-with-parent", "--chdir", str(cwd), "--"]
        argv += ["/bin/sh", "-c", command]
        return argv

    def bwrap_index(self, argv: list[str]) -> int | None:
        return bwrap_position(argv, self.bwrap)

    def _docker(self, command: str, *, cwd: Path, net: bool) -> list[str]:
        """Containerised run (cheap-model spec §8.3): pinned image, project mounted rw at
        /work, no network unless allowed, same uid so files stay owned by the user."""
        assert self.docker
        root = self.project_root.resolve()
        try:
            rel = cwd.resolve().relative_to(root)
            workdir = "/work" if str(rel) == "." else f"/work/{rel.as_posix()}"
        except ValueError:
            workdir = "/work"
        argv = [self.docker, "run", "--rm", "-i"]
        if not net:
            argv += ["--network", "none"]
        if hasattr(os, "getuid"):
            argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
        argv += ["-v", f"{root}:/work", "-w", workdir, self.config.docker_image]
        argv += ["/bin/sh", "-c", command]
        return argv

    def describe(self) -> str:
        if not self.active:
            return "off" if self.mode == "off" else "unavailable (bwrap not installed)"
        net = "network allowed" if self.config.allow_network else "no network"
        return (
            "bubblewrap · writable: project, /tmp"
            + (f", {len(self.config.writable_paths)} extra" if self.config.writable_paths else "")
            + f" · {net}"
        )

    @staticmethod
    def render(argv: list[str]) -> str:
        return " ".join(shlex.quote(a) for a in argv)
