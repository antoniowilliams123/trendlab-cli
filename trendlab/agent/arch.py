"""Architecture checks (uplift U24): import graph, cycles, layer rules, oversized modules.

Module-level imports of project modules form the graph (imports inside functions are the usual
deliberate way to break a cycle, so they are not edges). ``report`` gives the whole picture —
cycles (strongly connected components), rule violations, modules that are too big or that too
much depends on. ``new_violations`` judges a change: import edges it adds that break a rule or
close a new cycle.

Rules come from ``[governance] forbid_imports = ["pkg.low -> pkg.high", ...]`` (prefix match).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from trendlab.agent.symbols import module_file


def _module_name(root: Path, path: Path) -> str | None:
    rel = path.relative_to(root)
    parts = list(rel.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or None


def _resolve(root: Path, name: str) -> str | None:
    """The longest project module prefix of ``name`` (so `pkg.mod.func` → `pkg.mod`)."""
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        cand = ".".join(parts[:i])
        f = module_file(root, cand)
        if f is not None and f.is_file():
            return cand
    return None


def imports_of(root: Path, path: Path, source: str | None = None) -> set[str]:
    try:
        tree = ast.parse(source if source is not None else path.read_text(errors="replace"))
    except (SyntaxError, OSError):
        return set()
    me = _module_name(root, path) or ""
    pkg = me.split(".")[:-1] if path.name != "__init__.py" else me.split(".")
    out: set[str] = set()
    for node in tree.body:  # module level only
        if isinstance(node, ast.Import):
            for a in node.names:
                m = _resolve(root, a.name)
                if m:
                    out.add(m)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                mod = ".".join([*base, *([node.module] if node.module else [])])
            else:
                mod = node.module or ""
            for a in node.names:
                m = _resolve(root, f"{mod}.{a.name}") or _resolve(root, mod)
                if m:
                    out.add(m)
    out.discard(me)
    return out


def graph(root: Path, files: list[Path] | None = None) -> dict[str, set[str]]:
    files = (
        files
        if files is not None
        else [
            p
            for p in root.rglob("*.py")
            if not any(
                x in p.parts
                for x in (".venv", "venv", ".git", "node_modules", ".trendlab", "build", "dist")
            )
        ]
    )
    g: dict[str, set[str]] = {}
    for p in files:
        name = _module_name(root, p)
        if name and not name.startswith("tests"):
            g[name] = imports_of(root, p)
    return g


def cycles(g: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components with more than one module (Tarjan)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[list[str]] = []
    counter = [0]

    def visit(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in g.get(v, ()):
            if w not in g:
                continue
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                out.append(sorted(comp))

    import sys

    sys.setrecursionlimit(max(10_000, sys.getrecursionlimit()))
    for v in sorted(g):
        if v not in index:
            visit(v)
    return sorted(out, key=len, reverse=True)


def parse_rules(rules: list[str]) -> list[tuple[str, str]]:
    out = []
    for r in rules:
        if "->" in r:
            a, b = (x.strip() for x in r.split("->", 1))
            if a and b:
                out.append((a, b))
    return out


def _matches(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def violations(g: dict[str, set[str]], rules: list[tuple[str, str]]) -> list[str]:
    out = []
    for src, deps in sorted(g.items()):
        for dst in sorted(deps):
            for a, b in rules:
                if _matches(src, a) and _matches(dst, b):
                    out.append(f"{src} imports {dst} (rule: {a} -> {b} is forbidden)")
    return out


def report(root: Path, rules: list[str] | None = None, *, big_lines: int = 800) -> dict[str, Any]:
    g = graph(root)
    fan_in: dict[str, int] = {m: 0 for m in g}
    for deps in g.values():
        for d in deps:
            if d in fan_in:
                fan_in[d] += 1
    sizes = {}
    for m in g:
        f = module_file(root, m)
        if f is not None and f.is_file():
            sizes[m] = sum(1 for ln in f.read_text(errors="replace").splitlines() if ln.strip())
    cyc = cycles(g)
    return {
        "modules": len(g),
        "edges": sum(len(v) for v in g.values()),
        "cycles": cyc,
        "violations": violations(g, parse_rules(rules or [])),
        "big_modules": sorted(
            ((m, n) for m, n in sizes.items() if n > big_lines), key=lambda x: -x[1]
        ),
        "most_depended_on": sorted(fan_in.items(), key=lambda x: -x[1])[:5],
    }


def new_violations(
    root: Path,
    changed: dict[str, str],
    rules: list[str] | None = None,
    *,
    before_sources: dict[str, str | None] | None = None,
) -> list[str]:
    """Import edges a change adds that break a rule or close a new cycle.

    ``changed`` maps edited files (rel path) to their new source; ``before_sources`` to their
    source before the change (None = the file did not exist). Without ``before_sources`` the
    files on disk are taken as the "before" state."""
    disk = graph(root)
    before = {k: set(v) for k, v in disk.items()}
    after = {k: set(v) for k, v in disk.items()}
    for rel, source in changed.items():
        if not rel.endswith(".py"):
            continue
        name = _module_name(root, root / rel)
        if not name or name.startswith("tests"):
            continue
        after[name] = imports_of(root, root / rel, source)
        if before_sources is not None:
            old = before_sources.get(rel)
            if old is None:
                before.pop(name, None)
            else:
                before[name] = imports_of(root, root / rel, old)
    problems = []
    added_edges = {(a, b) for a, deps in after.items() for b in deps} - {
        (a, b) for a, deps in before.items() for b in deps
    }
    for a, b in parse_rules(rules or []):
        for src, dst in sorted(added_edges):
            if _matches(src, a) and _matches(dst, b):
                problems.append(f"new import {src} -> {dst} breaks rule {a} -> {b}")
    old_cycles = {tuple(c) for c in cycles(before)}
    for c in cycles(after):
        if tuple(c) not in old_cycles and any(src in c and dst in c for src, dst in added_edges):
            problems.append("new import cycle: " + " -> ".join(c))
    return problems
