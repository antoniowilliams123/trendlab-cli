"""Code-health snapshots and degradation trend (uplift U14).

A snapshot is static and fast (no tests run): source and test lines, test-to-source ratio,
Python function length and branch complexity from the AST, duplicated code windows, debt
markers and dependency count. Snapshots are appended per project; ``trend`` compares the newest
with an older one and names what got worse. ``backfill`` measures past commits from git so a
trend exists from day one, and the engine takes one snapshot a day and files an inbox card when
the project degrades.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

SOURCE_EXT = (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".rb")
_MARKER = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
_BRANCH = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ExceptHandler,
    ast.With,
    ast.AsyncWith,
    ast.BoolOp,
    ast.IfExp,
    ast.comprehension,
    ast.Match,
)
LONG_FUNCTION = 60
COMPLEX_FUNCTION = 10
WINDOW = 6


def _files(root: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        out = [
            str(p.relative_to(root))
            for p in root.rglob("*")
            if p.is_file() and ".git" not in p.parts and ".venv" not in p.parts
        ]
    return [f for f in out if f.endswith(SOURCE_EXT)]


def _is_test(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return (
        rel.startswith(("tests/", "test/"))
        or "/tests/" in rel
        or name.startswith("test_")
        or name.endswith(("_test.py", "_test.go", ".test.ts", ".test.js", ".spec.ts"))
    )


def _functions(tree: ast.AST) -> list[tuple[int, int]]:
    """(length in lines, branch complexity) per function."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            branches = sum(1 for n in ast.walk(node) if isinstance(n, _BRANCH))
            out.append((length, 1 + branches))
    return out


def snapshot(root: Path) -> dict[str, Any]:
    src_lines = test_lines = markers = 0
    funcs: list[tuple[int, int]] = []
    windows: Counter[str] = Counter()
    n_src = n_test = 0
    for rel in _files(root):
        try:
            text = (root / rel).read_text(errors="ignore")
        except OSError:
            continue
        lines = [ln.strip() for ln in text.splitlines()]
        code = [ln for ln in lines if ln and not ln.startswith(("#", "//"))]
        if _is_test(rel):
            n_test += 1
            test_lines += len(code)
            continue
        n_src += 1
        src_lines += len(code)
        markers += len(_MARKER.findall(text))
        for i in range(0, max(0, len(code) - WINDOW + 1)):
            chunk = "\n".join(code[i : i + WINDOW])
            if len(chunk) > 120:  # ignore trivial blocks (imports, braces)
                windows[hashlib.md5(chunk.encode()).hexdigest()] += 1
        if rel.endswith(".py"):
            try:
                funcs += _functions(ast.parse(text))
            except SyntaxError:
                pass
    dup = sum(c - 1 for c in windows.values() if c > 1)
    lengths = sorted(f[0] for f in funcs)
    return {
        "source_files": n_src,
        "test_files": n_test,
        "source_lines": src_lines,
        "test_lines": test_lines,
        "test_ratio": round(test_lines / src_lines, 3) if src_lines else None,
        "functions": len(funcs),
        "long_functions": sum(1 for f in funcs if f[0] > LONG_FUNCTION),
        "complex_functions": sum(1 for f in funcs if f[1] > COMPLEX_FUNCTION),
        "long_function_share": round(sum(1 for f in funcs if f[0] > LONG_FUNCTION) / len(funcs), 4)
        if funcs
        else None,
        "complex_function_share": round(
            sum(1 for f in funcs if f[1] > COMPLEX_FUNCTION) / len(funcs), 4
        )
        if funcs
        else None,
        "debt_per_kloc": round(markers / (src_lines / 1000), 3) if src_lines else None,
        "max_function_lines": lengths[-1] if lengths else 0,
        "mean_complexity": round(sum(f[1] for f in funcs) / len(funcs), 2) if funcs else None,
        "duplicated_windows": dup,
        "duplication": round(dup / max(1, src_lines // WINDOW), 4),
        "debt_markers": markers,
        "dependencies": _dependencies(root),
    }


def _dependencies(root: Path) -> int:
    n = 0
    py = root / "pyproject.toml"
    if py.is_file():
        try:
            import tomllib

            data = tomllib.loads(py.read_text())
            n += len(data.get("project", {}).get("dependencies", []))
        except (ValueError, OSError):
            pass
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            n += len(json.loads(pkg.read_text()).get("dependencies", {}))
        except (ValueError, OSError):
            pass
    return n


# metric → (direction that is worse, relative change that counts as degradation)
# size-adjusted: a growing codebase is not degradation, a worse share of it is
WATCH = {
    "test_ratio": ("down", 0.10),
    "long_function_share": ("up", 0.15),
    "complex_function_share": ("up", 0.15),
    "mean_complexity": ("up", 0.10),
    "duplication": ("up", 0.25),
    "debt_per_kloc": ("up", 0.25),
    "dependencies": ("up", 0.0001),
}


COUNTS = {"dependencies"}


def trend(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """What changed between two snapshots and which changes are degradation.

    A count metric is worse when it rises by at least one AND by more than its threshold; a
    ratio metric when it moves the bad way by more than its threshold (relative)."""
    worse, better, deltas = [], [], {}
    for key, (bad_dir, threshold) in WATCH.items():
        a, b = old.get(key), new.get(key)
        if a is None or b is None:
            continue
        deltas[key] = round(b - a, 4)
        sign = 1 if bad_dir == "up" else -1
        moved = sign * (b - a)  # > 0 means it got worse
        relative = moved / (abs(a) if a else 1.0)
        if key in COUNTS and abs(b - a) < 1:
            continue
        if relative > threshold:
            worse.append(key)
        elif relative < -threshold:
            better.append(key)
    growth = (new.get("source_lines") or 0) - (old.get("source_lines") or 0)
    return {
        "deltas": deltas,
        "worse": worse,
        "better": better,
        "source_growth": growth,
        "degraded": bool(worse),
    }


def history_path(root: Path) -> Path:
    from trendlab.config.loader import trendlab_home

    key = hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:10]
    return trendlab_home() / "engine" / "health" / f"{root.name}-{key}.jsonl"


def record(root: Path, snap: dict[str, Any], **meta: Any) -> None:
    p = history_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({**meta, **snap}) + "\n")


def history(root: Path) -> list[dict[str, Any]]:
    p = history_path(root)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def backfill(root: Path, commits: int = 10, step: int = 5) -> list[dict[str, Any]]:
    """Snapshots of past commits (every ``step``-th of the last ``commits * step``), oldest
    first, measured from ``git archive`` so the working tree is never touched."""
    shas = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "rev-list",
            "--first-parent",
            f"--max-count={commits * step}",
            "HEAD",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[::step][::-1]
    out = []
    for sha in shas:
        with tempfile.TemporaryDirectory(prefix="trendlab-health-") as tmp:
            arch = subprocess.run(
                ["git", "-C", str(root), "archive", sha], capture_output=True, check=True
            ).stdout
            subprocess.run(["tar", "-x", "-C", tmp], input=arch, check=True)
            subprocess.run(["git", "init", "-q", tmp], check=True)
            subprocess.run(["git", "-C", tmp, "add", "-A"], check=True)
            date = subprocess.run(
                ["git", "-C", str(root), "show", "-s", "--format=%cI", sha],
                capture_output=True,
                text=True,
            ).stdout.strip()
            out.append({"commit": sha[:10], "date": date, **snapshot(Path(tmp))})
    return out


def baseline(hist: list[dict[str, Any]], days: int = 7) -> dict[str, Any] | None:
    """The newest snapshot at least ``days`` old (else the oldest one)."""
    from datetime import datetime, timedelta

    if not hist:
        return None
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    old = [h for h in hist if h.get("at") and datetime.fromisoformat(h["at"]) <= cutoff]
    return old[-1] if old else hist[0]


async def health_projects(inbox, projects: list[Path]) -> list[dict[str, Any]]:
    """Engine job: one snapshot per project per day; a degradation against the 7-day baseline
    becomes an inbox card on that project."""
    from datetime import datetime

    out = []
    for root in projects:
        if not root.is_dir():
            continue
        snap = snapshot(root)
        base = baseline(history(root))
        record(root, snap, at=datetime.now().astimezone().isoformat(timespec="seconds"))
        res: dict[str, Any] = {"project": str(root), "snapshot": snap}
        if base is not None:
            t = trend(base, snap)
            res["trend"] = t
            if t["degraded"]:
                card = inbox.record(
                    project=str(root),
                    title="code health degraded: " + ", ".join(t["worse"]),
                    signature="health|" + "|".join(sorted(t["worse"])),
                    source="health",
                    evidence=[f"{k}: {base.get(k)} -> {snap.get(k)}" for k in t["worse"]],
                    severity="med",
                )
                res["card"] = card["id"]
        out.append(res)
    return out
