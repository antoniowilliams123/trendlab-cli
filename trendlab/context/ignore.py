"""Ignore rules (spec §63): default exclusions + ``.gitignore`` + ``.trendlabignore``.

A small gitignore-style matcher: ``*`` / ``?`` globs, ``**``, trailing ``/`` for
directories, leading ``/`` for root anchoring, ``!`` negation, comments. Good
enough for repository mapping and search; not a full git reimplementation.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_EXCLUDES = [
    ".git/",
    "node_modules/",
    ".venv/",
    "venv/",
    "dist/",
    "build/",
    "__pycache__/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".pytest_cache/",
    ".tox/",
    ".idea/",
    ".vscode/",
    "*.pyc",
    "*.pyo",
    "*.so",
    "*.dylib",
    "*.dll",
    "*.exe",
    "*.o",
    "*.class",
    "*.jar",
    "*.zip",
    "*.tar",
    "*.gz",
    "*.7z",
    "*.rar",
    "*.pdf",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.ico",
    "*.mp4",
    "*.mp3",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.parquet",
    "*.db",
    "*.sqlite",
    ".trendlab/",
]


@dataclass
class _Rule:
    pattern: str
    negate: bool
    dir_only: bool
    anchored: bool
    regex: re.Pattern[str] = field(init=False)

    def __post_init__(self) -> None:
        pat = self.pattern
        if "/" not in pat.rstrip("/"):
            # No slash: matches at any depth.
            regex = ".*/" + fnmatch.translate(pat).replace("(?s:", "(?s:", 1)
            regex = "(?:.*/)?" + _glob_to_regex(pat)
        else:
            regex = _glob_to_regex(pat.lstrip("/"))
        self.regex = re.compile("^" + regex + "$")

    def matches(self, rel: str, is_dir: bool) -> bool:
        if self.dir_only and not is_dir:
            # A dir-only pattern still matches files *inside* that directory.
            parts = rel.split("/")
            return any(self.regex.match("/".join(parts[:i])) for i in range(1, len(parts)))
        if self.regex.match(rel):
            return True
        # Patterns also match anything under a matched directory.
        parts = rel.split("/")
        return any(self.regex.match("/".join(parts[:i])) for i in range(1, len(parts)))


def _glob_to_regex(pat: str) -> str:
    out = ""
    i = 0
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
            continue
        if pat.startswith("**", i):
            out += ".*"
            i += 2
            continue
        if c == "*":
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c == "[":
            j = pat.find("]", i)
            if j == -1:
                out += re.escape(c)
            else:
                out += pat[i : j + 1]
                i = j
        else:
            out += re.escape(c)
        i += 1
    return out


class IgnoreRules:
    def __init__(self, patterns: list[str]) -> None:
        self.rules: list[_Rule] = []
        for raw in patterns:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            negate = line.startswith("!")
            if negate:
                line = line[1:]
            dir_only = line.endswith("/")
            anchored = line.startswith("/")
            self.rules.append(_Rule(line.rstrip("/"), negate, dir_only, anchored))

    @classmethod
    def for_project(
        cls, root: Path, *, respect_gitignore: bool = True, extra: list[str] | None = None
    ) -> IgnoreRules:
        patterns = list(DEFAULT_EXCLUDES)
        for name, enabled in ((".gitignore", respect_gitignore), (".trendlabignore", True)):
            path = root / name
            if enabled and path.is_file():
                patterns.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
        patterns.extend(extra or [])
        return cls(patterns)

    def ignored(self, rel_path: str, is_dir: bool = False) -> bool:
        rel = rel_path.replace("\\", "/").strip("/")
        if not rel:
            return False
        result = False
        for rule in self.rules:
            if rule.matches(rel, is_dir):
                result = not rule.negate
        return result

    def walk(self, root: Path, *, max_files: int | None = None) -> list[Path]:
        """Files under ``root`` not ignored, sorted, pruning ignored directories early."""
        out: list[Path] = []
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                entries = sorted(current.iterdir(), key=lambda p: p.name)
            except (PermissionError, FileNotFoundError):
                continue
            for entry in entries:
                rel = entry.relative_to(root).as_posix()
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if not self.ignored(rel, is_dir=True):
                        stack.append(entry)
                elif not self.ignored(rel):
                    out.append(entry)
                    if max_files and len(out) >= max_files:
                        return sorted(out)
        return sorted(out)
