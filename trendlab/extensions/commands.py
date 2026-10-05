"""Custom slash commands from Markdown (spec §90).

A file ``~/.trendlab/commands/<name>.md`` or ``<project>/.trendlab/commands/<name>.md`` becomes
``/<name>``. The body is a prompt template:

* ``$ARGUMENTS`` — everything typed after the command
* ``$1`` … ``$9`` — individual (shell-split) arguments
* ``@path`` references are expanded like in any other prompt (see ``trendlab.ui.file_refs``)

Optional YAML-ish front matter sets ``description`` (shown in /help) and ``model``::

    ---
    description: Review the current diff for security problems
    ---
    Review the diff below for injection, auth and secret-handling issues.
    $ARGUMENTS

Project commands override global ones of the same name. Built-in commands always win.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


@dataclass
class CustomCommand:
    name: str
    description: str
    body: str
    path: Path
    source: str  # "project" | "global"
    model: str | None = None
    meta: dict[str, str] = field(default_factory=dict)

    def render(self, args: list[str]) -> str:
        text = self.body
        joined = " ".join(args)
        for i in range(9, 0, -1):
            text = text.replace(f"${i}", args[i - 1] if i <= len(args) else "")
        text = text.replace("$ARGUMENTS", joined)
        if (
            joined
            and "$ARGUMENTS" not in self.body
            and not any(f"${i}" in self.body for i in range(1, 10))
        ):
            text = text.rstrip() + "\n\n" + joined
        return text.strip()


def parse_command_file(path: Path, source: str) -> CustomCommand | None:
    if not _NAME.match(path.stem):
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta: dict[str, str] = {}
    m = _FRONT.match(raw)
    body = raw
    if m:
        body = raw[m.end() :]
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip().strip("\"'")
    body = body.strip()
    if not body:
        return None
    description = meta.get("description") or next(
        (ln.strip("# ").strip() for ln in body.splitlines() if ln.strip()), path.stem
    )
    return CustomCommand(
        name=path.stem,
        description=description[:120],
        body=body[:20_000],
        path=path,
        source=source,
        model=meta.get("model") or None,
        meta=meta,
    )


class CustomCommandLibrary:
    def __init__(self, project_root: Path, home: Path) -> None:
        self.project_dir = project_root / ".trendlab" / "commands"
        self.global_dir = home / "commands"
        self._commands: dict[str, CustomCommand] = {}
        self.reload()

    def reload(self) -> None:
        self._commands.clear()
        for base, source in ((self.global_dir, "global"), (self.project_dir, "project")):
            if not base.is_dir():
                continue
            for md in sorted(base.glob("*.md")):
                cmd = parse_command_file(md, source)
                if cmd is not None:
                    self._commands[cmd.name] = cmd  # project (second) overrides global

    def list(self) -> list[CustomCommand]:
        return sorted(self._commands.values(), key=lambda c: c.name)

    def get(self, name: str) -> CustomCommand | None:
        return self._commands.get(name.lstrip("/").lower())

    def render(self, name: str, arg_text: str) -> str | None:
        cmd = self.get(name)
        if cmd is None:
            return None
        try:
            args = shlex.split(arg_text)
        except ValueError:
            args = arg_text.split()
        return cmd.render(args)

    def help_lines(self) -> list[str]:
        return [f"/{c.name:<24} {c.description}  [{c.source}]" for c in self.list()]

    def scaffold(self, name: str, *, project: bool = True) -> Path:
        """Write a starter command file and return its path."""
        if not _NAME.match(name):
            raise ValueError("command names are lowercase letters, digits, '-' or '_'")
        base = self.project_dir if project else self.global_dir
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{name}.md"
        if not path.exists():
            path.write_text(
                "---\n"
                f"description: Describe what /{name} does\n"
                "---\n"
                "Write the prompt here. $ARGUMENTS is replaced by whatever follows the command;\n"
                "reference files with @path/to/file.\n",
                encoding="utf-8",
            )
        self.reload()
        return path
