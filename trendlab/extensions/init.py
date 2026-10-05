"""``/init`` (spec §57): inspect the project and draft TRENDLAB.md for the user to review."""

from __future__ import annotations

import json
from pathlib import Path


def detect_project(root: Path) -> dict[str, list[str] | str | None]:
    langs: list[str] = []
    pm: str | None = None
    fw: list[str] = []
    fmt: list[str] = []
    lint: list[str] = []
    types: list[str] = []
    build: list[str] = []
    py = (
        (root / "pyproject.toml").read_text(errors="replace")
        if (root / "pyproject.toml").is_file()
        else ""
    )
    if py or (root / "requirements.txt").is_file() or list(root.glob("*.py")):
        langs.append("Python")
        pm = "uv" if (root / "uv.lock").is_file() else "poetry" if "[tool.poetry]" in py else "pip"
        if "pytest" in py or (root / "tests").is_dir():
            fw.append("pytest")
        if "ruff" in py:
            fmt.append("ruff format")
            lint.append("ruff check")
        if "black" in py:
            fmt.append("black")
        if "mypy" in py:
            types.append("mypy")
        if "pyright" in py:
            types.append("pyright")
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(errors="replace"))
        except ValueError:
            data = {}
        langs.append("TypeScript" if (root / "tsconfig.json").is_file() else "JavaScript")
        pm = (
            "pnpm"
            if (root / "pnpm-lock.yaml").is_file()
            else "yarn"
            if (root / "yarn.lock").is_file()
            else "npm"
        )
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        for k in ("jest", "vitest", "mocha"):
            if k in deps:
                fw.append(k)
        if "eslint" in deps:
            lint.append("eslint")
        if "prettier" in deps:
            fmt.append("prettier")
        if (root / "tsconfig.json").is_file():
            types.append("tsc")
        if "build" in data.get("scripts", {}):
            build.append(f"{pm} run build")
    if (root / "Cargo.toml").is_file():
        langs.append("Rust")
        pm = "cargo"
        fw.append("cargo test")
        lint.append("cargo clippy")
        build.append("cargo build")
    if (root / "go.mod").is_file():
        langs.append("Go")
        pm = "go"
        fw.append("go test ./...")
        build.append("go build ./...")
    return {
        "languages": langs,
        "package_manager": pm,
        "test_frameworks": fw,
        "formatters": fmt,
        "linters": lint,
        "type_checkers": types,
        "build": build,
    }


def draft_instructions(root: Path, validation: dict[str, str]) -> str:
    info = detect_project(root)
    lines = ["# TrendLab Project Instructions", "", f"Project: {root.name}", ""]
    if info["languages"]:
        lines.append(f"- Languages: {', '.join(info['languages'])}.")
    if info["package_manager"]:
        lines.append(f"- Package manager: {info['package_manager']}.")
    for key, label in (
        ("test_frameworks", "Tests"),
        ("formatters", "Formatting"),
        ("linters", "Linting"),
        ("type_checkers", "Type checking"),
        ("build", "Build"),
    ):
        if info[key]:
            lines.append(f"- {label}: {', '.join(info[key])}.")
    lines.append("")
    lines.append("## Validation commands (edit as needed)")
    for kind in ("test", "lint", "typecheck", "build"):
        lines.append(f"- {kind}: `{validation.get(kind, '(not detected)')}`")
    lines += [
        "",
        "## Working rules",
        "- Read before editing; prefer targeted patches over rewrites.",
        "- Run the relevant tests after every code change.",
        "- Never disable or delete tests to make the suite pass.",
        "- Do not add dependencies without asking.",
    ]
    return "\n".join(lines) + "\n"
