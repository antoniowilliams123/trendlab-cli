"""Hallucinated reference check (uplift U17): imports and project APIs that do not exist.

Cheap models invent plausible names: ``from shop.util import chunk_list`` when the function is
``chunks``, ``import requests_cache`` in a project that never installed it. After a Python edit
this module resolves, statically and without running anything:

* every ``import`` / ``from … import`` against the standard library, the project's own modules
  (flat and ``src/`` layouts, relative imports) and the project's virtualenv / declared
  dependencies;
* every name imported *from a project module* against that module's top-level definitions;
* every ``alias.attr`` call where ``alias`` is a project module.

Only lines the edit added are reported, so existing code is never nagged about.
"""

from __future__ import annotations

import ast
import re
import sys
from functools import lru_cache
from pathlib import Path

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def added_lines(diff: str) -> set[int]:
    """New-file line numbers of the ``+`` lines in a unified diff."""
    out: set[int] = set()
    line = 0
    for ln in (diff or "").splitlines():
        m = _HUNK.match(ln)
        if m:
            line = int(m.group(1))
            continue
        if ln.startswith("+++") or ln.startswith("---"):
            continue
        if ln.startswith("+"):
            out.add(line)
            line += 1
        elif ln.startswith("-"):
            continue
        elif line:
            line += 1
    return out


def _roots(root: Path) -> list[Path]:
    return [root, root / "src"] if (root / "src").is_dir() else [root]


def module_file(root: Path, dotted: str) -> Path | None:
    parts = dotted.split(".")
    for base in _roots(root):
        p = base.joinpath(*parts)
        if p.with_suffix(".py").is_file():
            return p.with_suffix(".py")
        if (p / "__init__.py").is_file():
            return p / "__init__.py"
        if p.is_dir() and any(p.glob("*.py")):  # namespace package
            return p
    return None


@lru_cache(maxsize=64)
def _site_packages(root: str) -> tuple[str, ...]:
    names: set[str] = set()
    for venv in (Path(root) / ".venv", Path(root) / "venv"):
        for sp in venv.glob("lib/python*/site-packages"):
            for entry in sp.iterdir():
                n = entry.name
                if n.endswith(".dist-info"):
                    top = entry / "top_level.txt"
                    if top.is_file():
                        names.update(t.strip() for t in top.read_text().splitlines() if t.strip())
                    names.add(n.split("-")[0].lower().replace("-", "_"))
                else:
                    names.add(n.removesuffix(".py").split(".")[0])
    return tuple(sorted(names))


@lru_cache(maxsize=64)
def _declared(root: str) -> tuple[str, ...]:
    names: set[str] = set()
    py = Path(root) / "pyproject.toml"
    if py.is_file():
        try:
            import tomllib

            data = tomllib.loads(py.read_text())
            deps = list(data.get("project", {}).get("dependencies", []))
            for extra in data.get("project", {}).get("optional-dependencies", {}).values():
                deps += extra
            for d in deps:
                names.add(re.split(r"[<>=!~\[; ]", d, maxsplit=1)[0].lower().replace("-", "_"))
        except (ValueError, OSError):
            pass
    req = Path(root) / "requirements.txt"
    if req.is_file():
        for line in req.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "-")):
                names.add(re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].lower().replace("-", "_"))
    return tuple(sorted(names))


def _external_ok(root: Path, top: str) -> bool | None:
    """True = resolves; False = not found anywhere we can see; None = cannot tell."""
    if top in sys.stdlib_module_names or top == "__future__":
        return True
    venv = _site_packages(str(root))
    if venv:
        return top in venv or top.lower() in venv or top.lower() in _declared(str(root))
    if top.lower() in _declared(str(root)):
        return True
    return None  # no venv to look in: do not guess


@lru_cache(maxsize=256)
def _defined(path: str, mtime: float) -> tuple[frozenset[str], bool]:
    """Top-level names a module defines, and whether it is open-ended (star import,
    module ``__getattr__``) so that missing names cannot be judged."""
    try:
        tree = ast.parse(Path(path).read_text(errors="replace"))
    except (SyntaxError, OSError):
        return frozenset(), True
    names: set[str] = set()
    open_ended = False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
            if node.name == "__getattr__":
                open_ended = True
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "*":
                    open_ended = True
                names.add(a.asname or a.name)
        elif isinstance(node, ast.If | ast.Try | ast.With):
            for sub in ast.walk(node):
                if isinstance(sub, ast.FunctionDef | ast.ClassDef):
                    names.add(sub.name)
                elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    names.add(sub.id)
                elif isinstance(sub, ast.alias):
                    names.add((sub.asname or sub.name).split(".")[0])
    return frozenset(names), open_ended


def _module_names(path: Path) -> tuple[frozenset[str], bool]:
    if path.is_dir():
        return frozenset(p.stem for p in path.glob("*.py")) | frozenset(
            d.name for d in path.iterdir() if d.is_dir()
        ), True
    names, open_ended = _defined(str(path), path.stat().st_mtime)
    if path.name == "__init__.py":  # submodules count as attributes of a package
        names = (
            names
            | frozenset(p.stem for p in path.parent.glob("*.py"))
            | frozenset(d.name for d in path.parent.iterdir() if (d / "__init__.py").is_file())
        )
    return names, open_ended


def _package_of(root: Path, rel: str) -> list[str]:
    parts = Path(rel).with_suffix("").parts
    if parts and parts[0] == "src":
        parts = parts[1:]
    return list(parts[:-1])


def unresolved(root: Path, rel: str, only_lines: set[int] | None = None) -> list[str]:
    """Problems in ``rel`` (lines limited to ``only_lines`` when given)."""
    path = root / rel
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except (SyntaxError, OSError):
        return []  # the parse postcondition reports this
    problems: list[str] = []
    aliases: dict[str, Path] = {}  # local name → project module file

    def report(node: ast.AST, msg: str) -> None:
        if only_lines is None or getattr(node, "lineno", 0) in only_lines:
            problems.append(f"{rel}:{node.lineno}: {msg}")

    guarded: set[int] = set()  # imports inside try/except ImportError are optional on purpose
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(
            h.type is None
            or any(
                isinstance(n, ast.Name)
                and n.id in {"ImportError", "ModuleNotFoundError", "Exception"}
                for n in ast.walk(h.type)
            )
            for h in node.handlers
        ):
            for sub in node.body:
                for n in ast.walk(sub):
                    if isinstance(n, ast.Import | ast.ImportFrom):
                        guarded.add(n.lineno)

    def project_top(top: str) -> bool:
        return any((b / top).exists() or (b / f"{top}.py").exists() for b in _roots(root))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if project_top(top):
                    f = module_file(root, a.name)
                    if f is None:
                        report(node, f"module {a.name} does not exist in this project")
                    elif a.asname:
                        aliases[a.asname] = f
                    elif "." not in a.name:
                        aliases[a.name] = f
                elif node.lineno not in guarded and _external_ok(root, top) is False:
                    report(node, f"module {a.name} is not installed or declared")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                pkg = _package_of(root, rel)
                base = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                dotted = ".".join([*base, *(node.module.split(".") if node.module else [])])
                if not dotted:
                    continue
            else:
                dotted = node.module or ""
                top = dotted.split(".")[0]
                if not project_top(top):
                    if node.lineno not in guarded and _external_ok(root, top) is False:
                        report(node, f"module {dotted} is not installed or declared")
                    continue
            f = module_file(root, dotted)
            if f is None:
                report(node, f"module {dotted} does not exist in this project")
                continue
            names, open_ended = _module_names(f)
            for a in node.names:
                if a.name == "*":
                    continue
                sub = module_file(root, f"{dotted}.{a.name}")
                if sub is not None:
                    aliases[a.asname or a.name] = sub
                elif not open_ended and a.name not in names:
                    report(node, f"{dotted} has no name {a.name!r}")
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
            and not node.attr.startswith("__")
        ):
            f = aliases[node.value.id]
            names, open_ended = _module_names(f)
            if not open_ended and node.attr not in names:
                report(node, f"{node.value.id} ({f.name}) has no attribute {node.attr!r}")
    seen, out = set(), []
    for p in problems:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out
