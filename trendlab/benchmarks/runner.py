"""Benchmark runner (spec §54): same harness, different models, objective metrics."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from rich.console import Console

from trendlab.benchmarks.fixtures import EXPECTED_CHANGED, FIXTURES, materialize
from trendlab.config.loader import load_config
from trendlab.config.schema import AppConfig, PermissionMode
from trendlab.telemetry.events import EventType


def suite_passes(root: Path) -> bool:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],  # noqa: S603
        cwd=root,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    return proc.returncode == 0


async def run_fixture(
    name: str,
    model: str,
    *,
    config: AppConfig | None = None,
    provider=None,
    home: Path | None = None,
) -> dict[str, Any]:
    from trendlab.app import TrendLabApp

    config = config or load_config()
    config.defaults.permission_mode = PermissionMode.TRUSTED  # benchmarks run unattended
    config.limits.max_iterations = min(config.limits.max_iterations, 40)
    with tempfile.TemporaryDirectory(prefix=f"trendlab-bench-{name}-") as tmp:
        root = Path(tmp) / "repo"
        prompt = materialize(name, root)
        before = suite_passes(root)
        tl = TrendLabApp(
            root,
            config,
            model_ref=model,
            provider=provider,
            console=Console(quiet=True),
            data_dir=home,
        )
        tool_calls = 0
        interventions = 0

        guards: dict[str, int] = {}

        def count(e):
            nonlocal tool_calls, interventions
            if e.type == EventType.TOOL_STARTED:
                tool_calls += 1
            if e.type == EventType.GUARD_FIRED:
                guards[e.data.get("guard")] = guards.get(e.data.get("guard"), 0) + 1
            if e.type == EventType.APPROVAL_REQUESTED:
                # Unattended: nobody can answer, so the request is denied at once (and counted)
                # instead of blocking the run until the approval times out.
                interventions += 1
                try:
                    tl.approvals.decide(
                        e.data["approval_id"], "deny", via="benchmark", trusted=True
                    )
                except Exception:  # noqa: BLE001
                    pass

        tl.events.subscribe(count)
        started = time.monotonic()
        await tl.start(interactive=False)
        try:
            result = await tl.run_prompt(prompt)
        finally:
            await tl.stop()
        after = suite_passes(root)
        changed = set(result.changed_files)
        lead = [r for r in (tl.costs.records if tl.costs else []) if r.role == "main"]
        tokens_lead = sum(r.input_tokens + r.output_tokens for r in lead)
        return {
            "fixture": name,
            "model": model,
            "success": after and result.status == "COMPLETED",
            "tests_before": before,
            "tests_after": after,
            "status": result.status,
            "model_calls": result.model_calls,
            "tool_calls": tool_calls,
            "iterations": result.iterations,
            "cost_usd": round(result.cost_usd, 4),
            "tokens_lead": tokens_lead,
            "cost_by_phase": {
                k: round(v["usd"], 4) for k, v in (tl.costs.by("phase") if tl.costs else {}).items()
            },
            "guards_fired": guards,
            "verification": (result.verification or {}).get("verdict"),
            "elapsed_s": round(time.monotonic() - started, 1),
            "files_changed": len(changed),
            "unnecessary_changes": sorted(changed - EXPECTED_CHANGED[name]),
            "human_interventions": interventions,
            "stop_reason": result.stop_reason,
        }


async def run_benchmarks(model: str, fixture: str | None = None, **kw) -> list[dict[str, Any]]:
    names = [fixture.upper()] if fixture else list(FIXTURES)
    results = []
    for name in names:
        results.append(await run_fixture(name, model, **kw))
    return results


if __name__ == "__main__":
    print(asyncio.run(run_benchmarks(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)))


# --------------------------------------------------------------------------------------------
# The 50-task suite, profiles and comparison (cheap-model spec §8)
# --------------------------------------------------------------------------------------------
PROFILES = {
    # the full cheap-model harness (tiering, planner, verifier, best-of-N, drivers)
    "harness": {},
    # bare = the harness features of this programme switched off; what a plain agent loop does
    "bare": {
        "context.tool_budgets": {},
        "planner.enabled": False,
        "verification.verifier": "off",
        "attempts.best_of": 1,
        "prompts.drivers": False,
    },
}


def apply_profile(config: AppConfig, profile: str) -> AppConfig:
    """``harness`` | ``bare``; a ``model@profile`` string sets the default model as well."""
    name = profile.split("@")[-1] if "@" in profile else profile
    if name not in PROFILES:
        raise ValueError(f"unknown profile {name!r}; choose from {', '.join(PROFILES)}")
    for dotted, value in PROFILES[name].items():
        section, key = dotted.split(".")
        setattr(getattr(config, section), key, value)
    return config


def _changed_lines(before: str, after: str) -> set[int]:
    """Line numbers (in ``before``) touched by the edit, from a line diff."""
    import difflib

    out: set[int] = set()
    sm = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines())
    for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        for ln in range(i1 + 1, max(i2, i1 + 1) + 1):
            out.add(ln)
    return out


async def run_task(
    task,
    model: str,
    *,
    config: AppConfig | None = None,
    provider=None,
    home: Path | None = None,
    profile: str = "harness",
    sandbox: str | None = None,
) -> dict[str, Any]:
    """One suite task → metrics (§8.2). ``sandbox='docker'`` runs commands in a pinned image."""
    import os
    import shutil

    from trendlab.app import TrendLabApp
    from trendlab.benchmarks import suite as suite_mod

    config = apply_profile(config or load_config(), profile)
    config.defaults.permission_mode = PermissionMode.UNSAFE  # unattended; approvals auto-denied
    config.limits.max_iterations = min(config.limits.max_iterations, 40)
    if sandbox:
        config.sandbox.mode = sandbox
        if sandbox == "docker":
            config.sandbox.docker_image = {
                "python": "python:3.12-slim",
                "typescript": "node:22-slim",
                "go": "golang:1.23",
            }[task.lang]
    if not suite_mod.toolchain_available(task.lang) and sandbox != "docker":
        return {
            "task": task.id,
            "lang": task.lang,
            "model": model,
            "profile": profile,
            "skipped": f"{suite_mod.TOOLCHAIN[task.lang]} not installed",
        }
    with tempfile.TemporaryDirectory(prefix=f"trendlab-suite-{task.id}-") as tmp:
        root = Path(tmp) / "repo"
        prompt = suite_mod.materialize(task, root)
        before = {f: (root / f).read_text() for f in task.expected_changed if (root / f).is_file()}
        tl = TrendLabApp(
            root,
            config,
            model_ref=model,
            provider=provider,
            console=Console(quiet=True),
            data_dir=home,
        )
        interventions = 0
        guards: dict[str, int] = {}

        def count(e):
            nonlocal interventions
            if e.type == EventType.GUARD_FIRED:
                guards[e.data.get("guard")] = guards.get(e.data.get("guard"), 0) + 1
            if e.type == EventType.APPROVAL_REQUESTED:
                interventions += 1
                try:
                    tl.approvals.decide(
                        e.data["approval_id"], "deny", via="benchmark", trusted=True
                    )
                except Exception:  # noqa: BLE001
                    pass

        tl.events.subscribe(count)
        started = time.monotonic()
        await tl.start(interactive=False)
        try:
            result = await tl.run_prompt(prompt)
        finally:
            await tl.stop()
        wall = time.monotonic() - started
        changed = set(result.changed_files)
        # hidden regression test decides "passes"
        suite_mod.write_hidden_test(task, root)
        env = dict(os.environ)
        proc = subprocess.run(
            task.test_command, shell=True, cwd=root, capture_output=True, text=True, env=env
        )
        passes = proc.returncode == 0
        shutil.rmtree(root / ".trendlab", ignore_errors=True)
        located = task.answer_file in changed
        root_cause = False
        if located and (root / task.answer_file).is_file():
            lines = _changed_lines(
                before.get(task.answer_file, ""), (root / task.answer_file).read_text()
            )
            root_cause = any(abs(ln - task.answer_line) <= 3 for ln in lines)
        test_like = [f for f in changed if "test" in f.lower()]
        lead = [r for r in (tl.costs.records if tl.costs else []) if r.role == "main"]
        return {
            "task": task.id,
            "lang": task.lang,
            "model": model,
            "profile": profile,
            "located": located,
            "root_cause": root_cause,
            "passes": passes,
            "no_collateral": (changed - set(task.expected_changed) - set(test_like)) == set(),
            "regression_added": bool(test_like),
            "interventions": interventions,
            "status": result.status,
            "cost": round(result.cost_usd, 4),
            "tokens_lead": sum(r.input_tokens + r.output_tokens for r in lead),
            "tokens_total": (tl.costs.total_input_tokens + tl.costs.total_output_tokens)
            if tl.costs
            else 0,
            "wall_s": round(wall, 1),
            "cost_by_phase": {
                k: round(v["usd"], 4) for k, v in (tl.costs.by("phase") if tl.costs else {}).items()
            },
            "guards_fired": guards,
            "verification": (result.verification or {}).get("verdict"),
            "stop_reason": result.stop_reason,
        }


def select_tasks(selector: str | None = None, lang: str | None = None) -> list:
    from trendlab.benchmarks.suite import TASKS

    tasks = [t for t in TASKS if not lang or t.lang == lang]
    if selector:
        if selector.isdigit():
            tasks = tasks[: int(selector)]
        else:
            wanted = {x.strip() for x in selector.split(",") if x.strip()}
            tasks = [t for t in tasks if t.id in wanted or any(t.id.startswith(w) for w in wanted)]
    return tasks


async def run_suite(
    model: str,
    *,
    profile: str = "harness",
    selector: str | None = None,
    lang: str | None = None,
    sandbox: str | None = None,
    on_result=None,
    **kw,
) -> list[dict[str, Any]]:
    out = []
    for task in select_tasks(selector, lang):
        r = await run_task(task, model, profile=profile, sandbox=sandbox, **kw)
        out.append(r)
        if on_result:
            on_result(r)
    return out


METRICS = ("located", "root_cause", "passes", "no_collateral", "regression_added")


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    ran = [r for r in results if not r.get("skipped")]
    n = len(ran) or 1
    summary: dict[str, Any] = {"tasks": len(ran), "skipped": len(results) - len(ran)}
    for m in METRICS:
        summary[m] = round(sum(1 for r in ran if r.get(m)) / n, 3)
    summary["interventions"] = sum(r.get("interventions", 0) for r in ran)
    summary["cost"] = round(sum(r.get("cost", 0.0) for r in ran), 4)
    summary["tokens_lead"] = sum(r.get("tokens_lead", 0) for r in ran)
    summary["wall_s"] = round(sum(r.get("wall_s", 0.0) for r in ran), 1)
    phases: dict[str, float] = {}
    for r in ran:
        for k, v in (r.get("cost_by_phase") or {}).items():
            phases[k] = round(phases.get(k, 0.0) + v, 4)
    summary["cost_by_phase"] = phases
    return summary


def compare_summaries(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Per-metric deltas b − a (§8.4)."""
    deltas = {}
    for k in (*METRICS, "interventions", "cost", "tokens_lead", "wall_s"):
        deltas[k] = round((b.get(k) or 0) - (a.get(k) or 0), 4)
    return deltas


def append_bench_log(path: Path, title: str, rows: list[dict[str, Any]], note: str = "") -> None:
    """docs/BENCH_LOG.md: one dated block per run, newest last (§8.4)."""
    from datetime import UTC, datetime

    lines = [f"\n## {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC — {title}\n"]
    if note:
        lines.append(note + "\n")
    keys = [k for k in rows[0] if k not in {"cost_by_phase"}] if rows else []
    if keys:
        lines.append("| " + " | ".join(keys) + " |")
        lines.append("|" + "---|" * len(keys))
        for r in rows:
            lines.append("| " + " | ".join(str(r.get(k, "")) for k in keys) + " |")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
