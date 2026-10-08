"""Mutation testing (uplift U25): which behaviour do the tests not pin down?

Small, classic mutations are applied one at a time to the functions under test — comparison
flips (``<`` ↔ ``<=``, ``==`` ↔ ``!=``), arithmetic swaps (``+`` ↔ ``-``), integer constants ±1,
``and`` ↔ ``or``, and an early ``return None``. The test command runs against each mutant: a
failing run *kills* it; a mutant that survives is behaviour no test checks — usually a missed
edge case. The file is always restored, even on error or interrupt.
"""

from __future__ import annotations

import ast
import copy
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CMP = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
}
_BIN = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.FloorDiv, ast.FloorDiv: ast.Mult}


@dataclass
class Mutant:
    line: int
    kind: str
    description: str
    source: str


def _functions(tree: ast.AST, only: set[str] | None):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
            only is None or node.name in only
        ):
            yield node


def mutants(source: str, *, functions: set[str] | None = None, limit: int = 20) -> list[Mutant]:
    """Mutants of ``source``, one change each, spread across the chosen functions."""
    tree = ast.parse(source)
    sites: list[tuple[int, str, str, Any]] = []
    for fn in _functions(tree, functions):
        for node in ast.walk(fn):
            if isinstance(node, ast.Compare) and type(node.ops[0]) in _CMP:
                new = _CMP[type(node.ops[0])]
                sites.append(
                    (
                        node.lineno,
                        "comparison",
                        f"{type(node.ops[0]).__name__} → {new.__name__}",
                        (node, "op", new),
                    )
                )
            elif isinstance(node, ast.BinOp) and type(node.op) in _BIN:
                new = _BIN[type(node.op)]
                sites.append(
                    (
                        node.lineno,
                        "arithmetic",
                        f"{type(node.op).__name__} → {new.__name__}",
                        (node, "bin", new),
                    )
                )
            elif isinstance(node, ast.BoolOp):
                new = ast.Or if isinstance(node.op, ast.And) else ast.And
                sites.append(
                    (
                        node.lineno,
                        "boolean",
                        f"{type(node.op).__name__} → {new.__name__}",
                        (node, "bool", new),
                    )
                )
            elif (
                isinstance(node, ast.Constant)
                and type(node.value) is int
                and not isinstance(node.value, bool)
            ):
                sites.append(
                    (
                        node.lineno,
                        "constant",
                        f"{node.value} → {node.value + 1}",
                        (node, "const", node.value + 1),
                    )
                )
        if fn.body and not isinstance(fn.body[0], ast.Return):
            sites.append(
                (
                    fn.body[0].lineno,
                    "early return",
                    f"{fn.name}() returns None at once",
                    (fn, "return", None),
                )
            )
    # spread: at most a few per line so one hot line does not take the whole budget
    seen_lines: dict[int, int] = {}
    chosen = []
    for site in sites:
        if site[1] == "early return":  # one per function, never crowded out
            chosen.append(site)
        elif seen_lines.get(site[0], 0) < 2:
            seen_lines[site[0]] = seen_lines.get(site[0], 0) + 1
            chosen.append(site)
        if len(chosen) >= limit:
            break
    out = []
    for line, kind, desc, (target, how, new) in chosen:
        t2 = copy.deepcopy(tree)
        # find the same node in the copy by position
        node = next(
            n
            for n in ast.walk(t2)
            if type(n) is type(target)
            and getattr(n, "lineno", None) == target.lineno
            and getattr(n, "col_offset", None) == target.col_offset
        )
        if how == "op":
            node.ops = [new()] + node.ops[1:]
        elif how == "bin":
            node.op = new()
        elif how == "bool":
            node.op = new()
        elif how == "const":
            node.value = new
        elif how == "return":
            node.body.insert(0, ast.Return(value=ast.Constant(value=None)))
        ast.fix_missing_locations(t2)
        out.append(Mutant(line, kind, desc, ast.unparse(t2)))
    return out


def run(
    root: Path,
    rel: str,
    test_command: str,
    *,
    functions: set[str] | None = None,
    limit: int = 20,
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    path = root / rel
    original = path.read_text()
    muts = mutants(original, functions=functions, limit=limit)
    killed, survived, errors = [], [], 0
    started = time.monotonic()
    try:
        for m in muts:
            path.write_text(m.source)
            try:
                proc = subprocess.run(
                    test_command,
                    shell=True,
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=env,
                )
                (killed if proc.returncode != 0 else survived).append(m)
            except subprocess.TimeoutExpired:
                killed.append(m)  # a hang is a detected change
                errors += 1
    finally:
        path.write_text(original)
    total = len(muts)
    return {
        "file": rel,
        "mutants": total,
        "killed": len(killed),
        "score": round(len(killed) / total, 3) if total else None,
        "survivors": [{"line": m.line, "kind": m.kind, "change": m.description} for m in survived],
        "timeouts": errors,
        "seconds": round(time.monotonic() - started, 1),
    }
