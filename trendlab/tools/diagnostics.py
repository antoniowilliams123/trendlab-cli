"""Post-edit diagnostics: run the project's linter/type checker on the files just changed and
hand the result straight back to the model, so mistakes surface immediately rather than at the
next test run."""

from __future__ import annotations

import asyncio
import shlex
import shutil
from pathlib import Path

from trendlab.config.schema import DiagnosticsConfig

DEFAULT_COMMANDS: dict[str, list[str]] = {
    ".py": ["ruff check --output-format concise {files}", "pyright --outputjson {files}"],
    ".ts": ["npx --no-install tsc --noEmit --pretty false", "npx --no-install eslint {files}"],
    ".tsx": ["npx --no-install tsc --noEmit --pretty false", "npx --no-install eslint {files}"],
    ".js": ["npx --no-install eslint {files}"],
    ".jsx": ["npx --no-install eslint {files}"],
    ".rs": ["cargo check --message-format short"],
    ".go": ["go vet ./..."],
}
MAX_OUTPUT = 4000


class Diagnostics:
    def __init__(self, config: DiagnosticsConfig, project_root: Path) -> None:
        self.config = config
        self.root = project_root

    def commands_for(self, files: list[str]) -> list[str]:
        if not self.config.enabled:
            return []
        by_ext: dict[str, list[str]] = {}
        for f in files:
            by_ext.setdefault(Path(f).suffix.lower(), []).append(f)
        cmds: list[str] = []
        for ext, names in by_ext.items():
            templates = self.config.commands.get(ext) or DEFAULT_COMMANDS.get(ext, [])
            quoted = " ".join(shlex.quote(n) for n in names)
            for t in templates:
                tool = shlex.split(t)[0] if t.strip() else ""
                if tool and tool != "npx" and shutil.which(tool) is None:
                    continue  # linter not installed: skip quietly
                cmds.append(t.format(files=quoted))
        return cmds

    async def run(self, files: list[str], *, argv_wrapper=None) -> str:
        """Return a short diagnostics report ('' when clean or nothing applies)."""
        cmds = self.commands_for(files)
        if not cmds:
            return ""
        reports: list[str] = []
        for cmd in cmds:
            argv = argv_wrapper(cmd) if argv_wrapper else ["/bin/sh", "-c", cmd]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(self.root),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                out, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=self.config.timeout_seconds
                )
            except TimeoutError:
                reports.append(f"$ {cmd}\n(timed out after {self.config.timeout_seconds}s)")
                continue
            except OSError as exc:
                reports.append(f"$ {cmd}\n({exc})")
                continue
            text = out.decode("utf-8", "replace").strip()
            if proc.returncode != 0 and text:
                reports.append(f"$ {cmd}\n{text[:MAX_OUTPUT]}")
        if not reports:
            return ""
        return "Diagnostics after edit (fix before moving on):\n" + "\n\n".join(reports)
