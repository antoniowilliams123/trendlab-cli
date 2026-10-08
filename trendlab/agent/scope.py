"""Scope and code-quality control (uplift U5).

Measures the *shape* of a change — files, lines, new definitions, new files, dependency edits —
against a budget derived from the task, so a small fix that balloons into a refactor, or a
quiet new dependency, is caught before the run is reported done. Also checks that a test the
model added actually fails without the fix (generated-test strength).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEP_FILES = {
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "Gemfile",
    "composer.json",
}
_DEF = re.compile(
    r"^\+\s*(?:async\s+def|def|class|function|func|fn|pub fn|export function)\s+(\w+)"
)
_DEP_LINE = re.compile(r'^\+\s*["\']?([A-Za-z0-9_.-]+)["\']?\s*(?:[=<>!~^]|:)')
_FIX = re.compile(
    r"\b(fix|bug|broken|fail(?:s|ing|ed)?|error|crash|regress|wrong|incorrect)\b", re.I
)


@dataclass
class ScopeReport:
    files: int = 0
    source_files: int = 0
    test_files: int = 0
    new_files: list[str] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    new_definitions: list[str] = field(default_factory=list)
    dependency_files: list[str] = field(default_factory=list)
    new_dependencies: list[str] = field(default_factory=list)
    budget_files: int = 0
    budget_definitions: int = 0
    task_kind: str = "change"  # fix | change
    problems: list[str] = field(default_factory=list)
    test_strength: str = "n/a"  # strong | weak | n/a | unknown
    test_gaming: list[str] = field(default_factory=list)  # CI bypass signs (U23)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_json(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "source_files": self.source_files,
            "test_files": self.test_files,
            "new_files": self.new_files,
            "added": self.added,
            "removed": self.removed,
            "new_definitions": self.new_definitions,
            "new_dependencies": self.new_dependencies,
            "budget_files": self.budget_files,
            "task_kind": self.task_kind,
            "problems": self.problems,
            "test_strength": self.test_strength,
            "test_gaming": self.test_gaming,
            "ok": self.ok,
        }


def is_test_path(path: str) -> bool:
    p = path.replace("\\", "/").lower()
    return bool(
        re.search(
            r"(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$",
            p,
        )
    )


_SKIP_ADDED = re.compile(
    r"@pytest\.mark\.(skip|xfail)|pytest\.(skip|xfail)\(|@unittest\.skip|\bxit\(|"
    r"\b(it|describe|test)\.skip\(|t\.Skip\(|#\[ignore\]"
)
_CI_FILE = re.compile(
    r"(^|/)(\.github/workflows/|\.gitlab-ci|pytest\.ini|tox\.ini|setup\.cfg|"
    r"pyproject\.toml|package\.json|Makefile|noxfile)"
)
_CI_LOOSEN = re.compile(
    r"\|\|\s*true|continue-on-error:\s*true|--deselect|-k\s+['\"]?not\s|"
    r"--no-verify|allow_failure:\s*true|exit 0"
)
_WEAKEN_OK = re.compile(r"\b(skip|xfail|disable|quarantine|flaky)\b", re.I)


def test_gaming(changed: dict[str, list[str]], task_text: str) -> list[str]:
    """Signs that tests or CI were weakened to make them pass (CI bypass, U23)."""
    if _WEAKEN_OK.search(task_text or ""):
        return []  # the task asked for it
    found: list[str] = []
    for path, diffs in changed.items():
        text = "\n".join(d for d in diffs if d)
        added = [
            ln[1:] for ln in text.splitlines() if ln.startswith("+") and not ln.startswith("+++")
        ]
        removed = [
            ln[1:] for ln in text.splitlines() if ln.startswith("-") and not ln.startswith("---")
        ]
        if is_test_path(path):
            if "+++ /dev/null" in text:
                found.append(f"{path}: test file deleted")
            if any(_SKIP_ADDED.search(a) for a in added):
                found.append(f"{path}: skip/xfail added")
            lost = sum(
                1 for r in removed if r.strip().startswith(("assert", "expect(", "self.assert"))
            )
            kept = sum(
                1 for a in added if a.strip().startswith(("assert", "expect(", "self.assert"))
            )
            if lost > kept:
                found.append(f"{path}: {lost - kept} assertion(s) removed")
        elif _CI_FILE.search(path) and any(_CI_LOOSEN.search(a) for a in added):
            found.append(f"{path}: CI/test command loosened")
    return found


def diff_shape(
    changed: dict[str, list[str]],
    task_text: str,
    *,
    max_files_fix: int = 4,
    max_files_change: int = 12,
    max_new_defs_fix: int = 3,
    dependency_gate: bool = True,
) -> ScopeReport:
    """Shape of the run's edits (from the per-edit unified diffs the tools recorded)."""
    rep = ScopeReport()
    rep.task_kind = "fix" if _FIX.search(task_text or "") else "change"
    rep.budget_files = max_files_fix if rep.task_kind == "fix" else max_files_change
    rep.budget_definitions = max_new_defs_fix if rep.task_kind == "fix" else 10**6
    task_lower = (task_text or "").lower()
    for path, diffs in changed.items():
        rep.files += 1
        if is_test_path(path):
            rep.test_files += 1
        else:
            rep.source_files += 1
        text = "\n".join(d for d in diffs if d)
        if "--- /dev/null" in text or "(new file)" in text:
            rep.new_files.append(path)
        for ln in text.splitlines():
            if ln.startswith("+++") or ln.startswith("---"):
                continue
            if ln.startswith("+"):
                rep.added += 1
                m = _DEF.match(ln)
                if m and not is_test_path(path):
                    rep.new_definitions.append(f"{path}:{m.group(1)}")
            elif ln.startswith("-"):
                rep.removed += 1
        name = path.rsplit("/", 1)[-1]
        if name in DEP_FILES:
            rep.dependency_files.append(path)
            for ln in text.splitlines():
                m = _DEP_LINE.match(ln)
                if m and m.group(1).lower() not in {
                    "version",
                    "name",
                    "description",
                    "python",
                    "scripts",
                    "dependencies",
                }:
                    dep = m.group(1)
                    if dep.lower() not in task_lower:
                        rep.new_dependencies.append(dep)
    if rep.source_files > rep.budget_files:
        rep.problems.append(
            f"{rep.source_files} source files changed for a {rep.task_kind} task "
            f"(budget {rep.budget_files})"
        )
    if len(rep.new_definitions) > rep.budget_definitions:
        rep.problems.append(
            f"{len(rep.new_definitions)} new functions/classes added for a fix "
            f"({', '.join(d.split(':')[-1] for d in rep.new_definitions[:4])}…)"
        )
    rep.test_gaming = test_gaming(changed, task_text)
    if rep.test_gaming:
        rep.problems.append(
            "tests or CI weakened instead of fixing the code: " + "; ".join(rep.test_gaming[:3])
        )
    if dependency_gate and rep.new_dependencies:
        rep.problems.append(
            "new dependency not requested by the task: " + ", ".join(rep.new_dependencies[:4])
        )
    return rep


def scope_nudge(rep: ScopeReport) -> str:
    return (
        "Scope check: "
        + "; ".join(rep.problems)
        + ". Either trim the change back to what the task needs (remove collateral edits, "
        "unrequested helpers or dependencies) or state in one line why each is required."
    )


class ChangeScopeRefused(Exception):
    """An edit outside ``[governance] change_allow`` (U9 change-scope constraint)."""

    def __init__(self, files: list[str], allow: list[str]) -> None:
        super().__init__(", ".join(files))
        self.files = files
        self.allow = allow


def outside_scope(files: list[str], allow: list[str], root: Path | None = None) -> list[str]:
    """Files (relative or absolute under ``root``) that match none of the ``allow`` globs."""
    if not allow:
        return []
    from fnmatch import fnmatch

    out = []
    for f in files:
        rel = f
        if root is not None:
            try:
                rel = (
                    str(Path(f).resolve().relative_to(root.resolve()))
                    if Path(f).is_absolute()
                    else f
                )
            except ValueError:
                rel = f
        rel = rel.replace("\\", "/").lstrip("./")
        if not any(fnmatch(rel, g) or fnmatch(rel, g.rstrip("/") + "/*") for g in allow):
            out.append(rel)
    return out
