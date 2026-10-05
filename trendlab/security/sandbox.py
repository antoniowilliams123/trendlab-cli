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

    @property
    def available(self) -> bool:
        return self.bwrap is not None

    @property
    def mode(self) -> str:
        """Effective mode: the TRENDLAB_SANDBOX env var (off/on/auto) overrides the config."""
        env = os.environ.get("TRENDLAB_SANDBOX", "").strip().lower()
        return env if env in {"off", "on", "auto"} else self.config.mode

    @property
    def active(self) -> bool:
        if self.mode == "off":
            return False
        if self.mode == "on" and not self.available:
            raise RuntimeError("sandbox mode is 'on' but bubblewrap (bwrap) is not installed")
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
        assert self.bwrap
        net = self.config.allow_network if allow_network is None else allow_network
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
