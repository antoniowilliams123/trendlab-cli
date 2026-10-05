"""Lightweight repository map (spec §19), cached by file mtimes in ``.trendlab/cache``."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from trendlab.context.ignore import IgnoreRules

_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".c": "c",
    ".sh": "shell",
    ".sql": "sql",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".md": "markdown",
    ".html": "html",
    ".css": "css",
}
_CONFIG_FILES = {
    "pyproject.toml",
    "setup.py",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Makefile",
    "Dockerfile",
    "docker-compose.yml",
    "requirements.txt",
    "tsconfig.json",
    "TRENDLAB.md",
}
_ENTRY_HINTS = {
    "main.py",
    "app.py",
    "cli.py",
    "__main__.py",
    "index.js",
    "index.ts",
    "main.rs",
    "main.go",
}


def _py_symbols(path: Path, limit: int = 12) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, OSError):
        return []
    out = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            out.append(f"class {node.name}")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            out.append(f"def {node.name}")
        if len(out) >= limit:
            break
    return out


class RepositoryMap:
    def __init__(self, root: Path, rules: IgnoreRules, *, max_files: int = 400) -> None:
        self.root = root
        self.rules = rules
        self.max_files = max_files
        self.cache_path = root / ".trendlab" / "cache" / "repo_map.json"
        self.files: list[str] = []
        self.symbols: dict[str, list[str]] = {}
        self.languages: dict[str, int] = {}
        self.config_files: list[str] = []
        self.entry_points: list[str] = []
        self.test_files: list[str] = []
        self.truncated = False

    def build(self, *, use_cache: bool = True) -> RepositoryMap:
        paths = self.rules.walk(self.root, max_files=self.max_files + 1)
        self.truncated = len(paths) > self.max_files
        paths = paths[: self.max_files]
        stamps = {p.relative_to(self.root).as_posix(): p.stat().st_mtime_ns for p in paths}
        cached = self._load_cache() if use_cache else None
        self.files = sorted(stamps)
        self.symbols = {}
        for rel in self.files:
            lang = _LANG.get(Path(rel).suffix)
            if lang:
                self.languages[lang] = self.languages.get(lang, 0) + 1
            name = Path(rel).name
            if name in _CONFIG_FILES:
                self.config_files.append(rel)
            if name in _ENTRY_HINTS:
                self.entry_points.append(rel)
            if "test" in name.lower() or rel.startswith(("tests/", "test/")):
                self.test_files.append(rel)
            if rel.endswith(".py"):
                if (
                    cached
                    and cached.get("stamps", {}).get(rel) == stamps[rel]
                    and rel in cached.get("symbols", {})
                ):
                    syms = cached["symbols"][rel]
                else:
                    syms = _py_symbols(self.root / rel)
                if syms:
                    self.symbols[rel] = syms
        self._save_cache(stamps)
        return self

    def _load_cache(self) -> dict | None:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save_cache(self, stamps: dict[str, int]) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps({"stamps": stamps, "symbols": self.symbols}), encoding="utf-8"
            )
        except OSError:
            pass

    def render(self, max_chars: int = 12_000) -> str:
        lines = []
        langs = ", ".join(
            f"{k} ({v})" for k, v in sorted(self.languages.items(), key=lambda kv: -kv[1])[:5]
        )
        if langs:
            lines.append(f"Languages: {langs}")
        if self.config_files:
            lines.append("Config: " + ", ".join(self.config_files[:8]))
        if self.entry_points:
            lines.append("Entry points: " + ", ".join(self.entry_points[:6]))
        if self.test_files:
            lines.append(
                f"Tests: {len(self.test_files)} files (e.g. {', '.join(self.test_files[:3])})"
            )
        lines.append("Files:")
        tree = _tree(self.files)
        for line in tree:
            lines.append(line)
            rel = line.strip()
            if rel in self.symbols:
                lines.append(
                    " " * (len(line) - len(line.lstrip()) + 2)
                    + "· "
                    + ", ".join(self.symbols[rel][:8])
                )
        if self.truncated:
            lines.append(
                f"... (map limited to {self.max_files} files; use glob/search_text for more)"
            )
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[: max_chars - 40] + "\n... (repository map truncated)"
        return text


def _tree(files: list[str]) -> list[str]:
    """Render paths as an indented tree; each file line's stripped text is its relative path."""
    out: list[str] = []
    seen_dirs: set[str] = set()
    for rel in files:
        parts = rel.split("/")
        for depth in range(len(parts) - 1):
            d = "/".join(parts[: depth + 1])
            if d not in seen_dirs:
                seen_dirs.add(d)
                out.append("  " * depth + parts[depth] + "/")
        out.append("  " * (len(parts) - 1) + rel)
    return out
