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


@dataclass
class Sandbox:
    config: SandboxConfig
    project_root: Path
    bwrap: str | None = field(default_factory=lambda: shutil.which("bwrap"))

    docker: str | None = field(default_factory=lambda: shutil.which("docker"))

    @property
    def available(self) -> bool:
        if self.mode == "docker":
            return self.docker is not None
        return self.bwrap is not None

    @property
    def mode(self) -> str:
        """Effective mode: the TRENDLAB_SANDBOX env var (off/on/auto) overrides the config."""
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
        if self.mode == "docker":
            return self._docker(command, cwd=cwd, net=net)
        assert self.bwrap
        argv = [
            self.bwrap,
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
        ]
        # Private /tmp first; binds after it so a project under /tmp is still mounted rw.
        argv += ["--tmpfs", "/tmp"]
        for p in self.writable_paths():
            argv += ["--bind", str(p), str(p)]
        argv += ["--unshare-user-try", "--unshare-pid", "--unshare-ipc", "--unshare-uts"]
        if not net:
            argv.append("--unshare-net")
        argv += ["--die-with-parent", "--chdir", str(cwd), "--"]
        argv += ["/bin/sh", "-c", command]
        return argv

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
