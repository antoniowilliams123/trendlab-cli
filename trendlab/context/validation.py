"""Automatic validation-command detection (spec §66). Config overrides detection."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from trendlab.config.schema import AppConfig

KINDS = ("test", "lint", "typecheck", "build")


def _python() -> str:
    return "python" if shutil.which("python") else "python3"


def detect_validation_commands(root: Path) -> dict[str, str]:
    cmds: dict[str, str] = {}
    py = (
        (root / "pyproject.toml").is_file()
        or (root / "setup.py").is_file()
        or any(root.glob("*.py"))
    )
    if py:
        text = (
            (root / "pyproject.toml").read_text(errors="replace")
            if (root / "pyproject.toml").is_file()
            else ""
        )
        if (root / "tests").is_dir() or "pytest" in text or (root / "pytest.ini").is_file():
            cmds["test"] = f"{_python()} -m pytest -q"
        if "ruff" in text or (root / "ruff.toml").is_file():
            cmds["lint"] = "ruff check ."
        elif "flake8" in text or (root / ".flake8").is_file():
            cmds["lint"] = "flake8"
        if "mypy" in text or (root / "mypy.ini").is_file():
            cmds["typecheck"] = "mypy ."
        elif "pyright" in text:
            cmds["typecheck"] = "pyright"
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text(errors="replace")).get("scripts", {})
        except ValueError:
            scripts = {}
        for kind, names in (
            ("test", ["test"]),
            ("lint", ["lint"]),
            ("typecheck", ["typecheck", "tsc"]),
            ("build", ["build"]),
        ):
            for n in names:
                if n in scripts and not is_placeholder_script(str(scripts[n])):
                    cmds.setdefault(kind, f"npm run {n}" if n != "test" else "npm test")
                    break
    if (root / "Cargo.toml").is_file():
        cmds.setdefault("test", "cargo test")
        cmds.setdefault("lint", "cargo clippy")
    if (root / "go.mod").is_file():
        cmds.setdefault("test", "go test ./...")
    if (root / "Makefile").is_file():
        mk = (root / "Makefile").read_text(errors="replace")
        if "\ntest:" in mk and "test" not in cmds:
            cmds["test"] = "make test"
    return cmds


# `npm init` writes a test script that only fails; others are just an echo. Neither tests.
_PLACEHOLDER = re.compile(
    r"""^\s*(echo\s+(["']).*?\2|echo\b[^&|;]*|true|:)?\s*(&&\s*exit\s+\d+)?\s*$""", re.I
)


def is_placeholder_script(script: str) -> bool:
    """True for a package.json script that runs nothing (the npm-init 'no test specified')."""
    s = script.strip()
    return not s or "no test specified" in s.lower() or bool(_PLACEHOLDER.match(s))


_MARKERS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "pytest.ini",
    "tox.ini",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Makefile",
)


def _has_tests(d: Path) -> bool:
    return (d / "tests").is_dir() or any(d.glob("test_*.py"))


def nearest_project(root: Path, rel_files) -> Path:
    """The folder whose tests cover the changed files: for each file, the closest folder up to
    ``root`` that has a project marker or a tests folder *and* a detectable test command. When
    the files live in different projects, the deepest folder that contains them all. Falls
    back to ``root``."""
    root = root.resolve()
    found: list[Path] = []
    for rel in rel_files or []:
        d = (root / rel).resolve().parent
        if root not in d.parents and d != root:
            continue
        while d != root:
            marked = any((d / m).exists() for m in _MARKERS) or _has_tests(d)
            if marked and "test" in detect_validation_commands(d):
                found.append(d)
                break
            d = d.parent
        else:
            found.append(root)
    if not found:
        return root
    common = found[0]
    for d in found[1:]:
        while common != root and common not in d.parents and common != d:
            common = common.parent
    return common


def validation_commands(config: AppConfig, root: Path) -> dict[str, str]:
    """Detected commands overridden by ``[project] test_command`` etc."""
    cmds = detect_validation_commands(root)
    for kind in KINDS:
        configured = config.project.get(f"{kind}_command")
        if configured:
            cmds[kind] = configured
    return cmds
