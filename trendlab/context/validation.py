"""Automatic validation-command detection (spec §66). Config overrides detection."""

from __future__ import annotations

import json
from pathlib import Path

from trendlab.config.schema import AppConfig

KINDS = ("test", "lint", "typecheck", "build")


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
            cmds["test"] = "python -m pytest -q"
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
                if n in scripts:
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


def validation_commands(config: AppConfig, root: Path) -> dict[str, str]:
    """Detected commands overridden by ``[project] test_command`` etc."""
    cmds = detect_validation_commands(root)
    for kind in KINDS:
        configured = config.project.get(f"{kind}_command")
        if configured:
            cmds[kind] = configured
    return cmds
