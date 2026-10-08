"""Benchmark runner (spec §54): same harness, different models, objective metrics."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from rich.console import Console

from trendlab.agent.taxonomy import by_code, classify_row
from trendlab.benchmarks.fixtures import EXPECTED_CHANGED, FIXTURES, materialize
from trendlab.benchmarks.unattended import Unattended
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
        guards: dict[str, int] = {}

        def count(e):
            nonlocal tool_calls
            if e.type == EventType.TOOL_STARTED:
                tool_calls += 1
            if e.type == EventType.GUARD_FIRED:
                guards[e.data.get("guard")] = guards.get(e.data.get("guard"), 0) + 1

        tl.events.subscribe(count)
        unattended = Unattended(tl)  # deny approvals, answer questions: nobody is watching
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
            "human_interventions": unattended.interventions,
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
    # the full cheap-model harness as configured (tiering, router, planner, verifier, scope,
    # best-of-N, driver notes)
    "harness": {},
    "harness+retrieval": {"context.retrieval": True},
    # bare = every harness feature of the programme switched off; a plain agent loop
    "bare": {
        "context.tool_budgets": {},
        "context.retrieval": False,
        "planner.enabled": False,
        "verification.verifier": "off",
        "attempts.best_of": 1,
        "prompts.drivers": False,
        "routing.router": None,
        "governance.scope_check": False,
    },
}
# Ablations: the harness minus exactly one component, to measure each part's contribution.
ABLATIONS = {
    "harness-no-tiering": {"context.tool_budgets": {}},
    "harness-no-router": {"routing.router": None},
    "harness-no-planner": {"planner.enabled": False},
    "harness-no-verifier": {"verification.verifier": "off"},
    "harness-no-scope": {"governance.scope_check": False},
    "harness-no-bestof": {"attempts.best_of": 1},
    "harness-no-drivers": {"prompts.drivers": False},
}
PROFILES.update(ABLATIONS)


def set_dotted(config: AppConfig, dotted: str, value: Any) -> None:
    """``section.key`` = value; dict sections (routing, pricing) set or remove (None) the key."""
    section, key = dotted.split(".", 1)
    target = getattr(config, section)
    if isinstance(target, dict):
        if value is None:
            target.pop(key, None)
        else:
            target[key] = value
        return
    if not hasattr(target, key):
        raise ValueError(f"unknown setting {dotted!r}")
    setattr(target, key, value)


def parse_override(text: str) -> tuple[str, Any]:
    """'verification.min_diff_lines=30' → ('verification.min_diff_lines', 30)."""
    import json as _json

    key, _, raw = text.partition("=")
    try:
        value = _json.loads(raw)
    except ValueError:
        value = raw
    return key.strip(), value


def apply_profile(config: AppConfig, profile: str) -> AppConfig:
    """A named profile (see PROFILES/ABLATIONS); ``model@profile`` strings are accepted, and
    ``profile+key=value[,key=value]`` adds overrides (sensitivity sweeps)."""
    name = profile.split("@")[-1] if "@" in profile else profile
    overrides = []
    if "+" in name and "=" in name.split("+", 1)[1]:
        name, extra = name.split("+", 1)
        overrides = [parse_override(x) for x in extra.split(",") if x]
    if name not in PROFILES:
        raise ValueError(f"unknown profile {name!r}; choose from {', '.join(PROFILES)}")
    for dotted, value in PROFILES[name].items():
        set_dotted(config, dotted, value)
    for dotted, value in overrides:
        set_dotted(config, dotted, value)
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
    perturbation: str | None = None,
    chaos: float = 0.0,
) -> dict[str, Any]:
    """One suite task → metrics (§8.2). ``sandbox='docker'`` runs commands in a pinned image;
    ``perturbation`` rewrites the prompt (typos | terse | verbose); ``chaos`` makes that share
    of model calls fail transiently."""
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
        if perturbation:
            from trendlab.benchmarks.perturb import perturb

            prompt = perturb(prompt, perturbation, seed=task.id)
        before = {
            str(p.relative_to(root)): p.read_text(errors="replace")
            for p in root.rglob("*")
            if p.is_file() and ".trendlab" not in p.parts
        }
        tl = TrendLabApp(
            root,
            config,
            model_ref=model,
            provider=provider,
            console=Console(quiet=True),
            data_dir=home,
        )
        chaos_wrappers: list[Any] = []
        guards: dict[str, int] = {}
        tools: dict[str, dict[str, int]] = {}
        commands: list[str] = []
        sequence: list[tuple[str, str]] = []  # (tool, path) in order, for tool-use metrics
        plan_files: list[str] = []
        plan_events: list[dict] = []
        route_kind: list[str] = []

        def count(e):
            if e.type == EventType.TOOL_STARTED:
                if e.data.get("command"):
                    commands.append(str(e.data["command"]))
                files = e.data.get("files") or []
                sequence.append((str(e.data.get("tool")), str(files[0]) if files else ""))
            if e.type == EventType.ROUTE_DECIDED:
                route_kind.append(str(e.data.get("kind")))
            if e.type == EventType.PLANNER_CALLED:
                plan_events.append(dict(e.data))
                for fs in e.data.get("files") or []:
                    plan_files.extend(fs)
            if e.type == EventType.GUARD_FIRED:
                guards[e.data.get("guard")] = guards.get(e.data.get("guard"), 0) + 1
            if e.type in {EventType.TOOL_COMPLETED, EventType.TOOL_SKIPPED}:
                name = str(e.data.get("tool"))
                d = tools.setdefault(name, {"calls": 0, "failed": 0, "skipped": 0})
                d["calls"] += 1
                if e.type == EventType.TOOL_SKIPPED:
                    d["skipped"] += 1
                elif not e.data.get("ok", True) and name not in {"shell", "run_tests"}:
                    # a non-zero exit from a command is a result, not a tool failure
                    d["failed"] += 1

        tl.events.subscribe(count)
        unattended = Unattended(tl)
        started = time.monotonic()
        await tl.start(interactive=False)
        if chaos > 0:
            # after start(): the gateway only exists once the app has started
            # keep the live list: wrappers created later by the factory are appended to it
            chaos_wrappers = install_chaos(tl.gateway, chaos, seed=task.id)
        try:
            result = await tl.run_prompt(prompt)
            for follow in task.followups:  # multi-turn: same session, same context
                result = await tl.run_prompt(follow)
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
        passes = proc.returncode == 0  # safety (adversarial tier) is reported separately as "safe"
        answered = None
        if task.answer_keywords:  # question task: right answer, nothing edited
            text = (result.text or "").lower()
            answered = all(
                any(alt.lower() in text for alt in k.split("|")) for k in task.answer_keywords
            )
            passes = bool(answered) and not changed
        violations = suite_mod.check_forbid(task, root, commands)
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
        import difflib
        import difflib as _difflib

        diff_parts = []
        for f in sorted(changed):
            after_text = (root / f).read_text(errors="replace") if (root / f).is_file() else ""
            before_text = before.get(f, "")
            diff_parts.append(
                "".join(
                    _difflib.unified_diff(
                        before_text.splitlines(True),
                        after_text.splitlines(True),
                        f"a/{f}",
                        f"b/{f}",
                    )
                )
            )
        run_diff = "\n".join(diff_parts)[:20_000]
        context_text = json.dumps(tl.context.messages if tl.context else [], default=str)
        leak = suite_mod.leakage(task, context_text, authored=run_diff)
        # tool-use eval: every edited file was read first; validation ran after the last edit
        edits = [
            i
            for i, (t, _p) in enumerate(sequence)
            if t in {"write_file", "patch_file", "apply_patch"}
        ]
        read_paths = {p for t, p in sequence if t == "read_file"}
        read_before_edit = all(
            sequence[i][1] in read_paths or sequence[i][0] == "write_file" for i in edits
        )
        validated_after_edit = not edits or any(
            t in {"run_tests", "shell"} for t, _p in sequence[edits[-1] + 1 :]
        )
        plan_recall = None
        if plan_files:
            planned = {p.strip("./") for p in plan_files}
            expected = {p.strip("./") for p in task.expected_changed}
            plan_recall = round(len(planned & expected) / len(expected), 3) if expected else None
        planning = plan_metrics(task, plan_events, changed)
        ref = suite_mod.reference_content(task, task.answer_file)
        final = (root / task.answer_file).read_text() if (root / task.answer_file).is_file() else ""
        exact_match = ref is not None and final == ref
        ref_similarity = (
            round(difflib.SequenceMatcher(None, ref, final).ratio(), 4) if ref is not None else None
        )
        return {
            "task": task.id,
            "lang": task.lang,
            "tier": task.tier,
            "answered": answered,
            "route": route_kind[0] if route_kind else None,
            "route_expected": suite_mod.expected_route(task),
            "safe": not violations,
            "violations": violations,
            "turns": 1 + len(task.followups),
            "perturbation": perturbation,
            "chaos_injected": sum(w.injected for w in chaos_wrappers),
            "read_before_edit": read_before_edit,
            "validated_after_edit": validated_after_edit,
            "plan_recall": plan_recall,
            **planning,
            "suite_version": suite_mod.SUITE_VERSION,
            "leakage": leak,
            "diff": run_diff,
            "prompt": task.prompt,
            "defect_kind": task.defect.kind,
            "symptom_only": "existing tests still pass" in task.prompt,
            "exact_match": exact_match,
            "ref_similarity": ref_similarity,
            "unsupported_claims": list(result.unsupported_claims),
            "verifier_confidence": (result.verification or {}).get("confidence"),
            "verifier_rubric": (result.verification or {}).get("rubric") or {},
            "model": model,
            "profile": profile,
            "located": located,
            "root_cause": root_cause,
            "passes": passes,
            "no_collateral": (
                changed - set(task.expected_changed) - set(task.allowed_changed) - set(test_like)
            )
            == set(),
            "regression_added": bool(test_like),
            "interventions": unattended.interventions,
            "questions_asked": unattended.questions_answered,
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
            "scope_ok": (result.scope or {}).get("ok"),
            "answer_words": (result.communication or {}).get("words"),
            "reading_ease": (result.communication or {}).get("reading_ease"),
            "robospeak": len((result.communication or {}).get("robospeak") or []),
            "undefined_acronyms": len((result.communication or {}).get("undefined_acronyms") or []),
            "test_strength": (result.scope or {}).get("test_strength"),
            "tool_calls": sum(d["calls"] for d in tools.values()),
            "tool_failures": sum(d["failed"] + d["skipped"] for d in tools.values()),
            "tools": tools,
            "model_version": model,
            "model_served": sorted({r.model_served for r in lead if r.model_served})[:1] or [""],
            "prompt_hash": sorted({r.prompt_hash for r in lead if r.prompt_hash})[:1] or [""],
        }


def select_tasks(
    selector: str | None = None,
    lang: str | None = None,
    tier: str = "base",
    *,
    holdout: bool = False,
) -> list:
    """``tier``: base, hard or all. Holdout tasks (U3) are excluded unless ``holdout`` is True,
    in which case only they are returned — tune on the rest, gate releases on these."""
    from trendlab.benchmarks.suite import TASKS

    tasks = [t for t in TASKS if not lang or t.lang == lang]
    tasks = [t for t in tasks if t.holdout == holdout]
    if tier != "all":
        tasks = [t for t in tasks if t.tier == tier]
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
    tier: str = "base",
    runs: int = 1,
    holdout: bool = False,
    concurrency: int = 1,
    **kw,
) -> list[dict[str, Any]]:
    """``runs`` > 1 repeats every task so pass@k, flakiness and intervals mean something;
    ``concurrency`` > 1 runs that many tasks at once (stress / load test of the harness)."""
    jobs = [
        (task, run)
        for task in select_tasks(selector, lang, tier, holdout=holdout)
        for run in range(max(1, runs))
    ]
    out: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(task, run):
        async with sem:
            t0 = time.monotonic()
            started_at = time.time()
            try:
                r = await run_task(task, model, profile=profile, sandbox=sandbox, **kw)
            except Exception as exc:  # noqa: BLE001 — a crash is a measured outcome under load
                r = {
                    "task": task.id,
                    "lang": task.lang,
                    "tier": task.tier,
                    "passes": False,
                    "status": "CRASHED",
                    "error": f"{exc.__class__.__name__}: {exc}"[:300],
                    "cost": 0.0,
                    "wall_s": round(time.monotonic() - t0, 1),
                }
            r["run"] = run + 1
            r["started_at"], r["ended_at"] = round(started_at, 2), round(time.time(), 2)
            r["failure_code"] = classify_row(r)
            out.append(r)
            if on_result:
                on_result(r)

    if concurrency <= 1:
        for task, run in jobs:
            await one(task, run)
    else:
        await asyncio.gather(*(one(t, r) for t, r in jobs))
    return out


def install_chaos(gateway, rate: float, *, seed: str) -> list[Any]:
    """Wrap every provider the gateway has or will create so ``rate`` of model calls fail
    transiently. Returns the live wrapper list (its ``injected`` counts are read after the run)."""
    from trendlab.benchmarks.perturb import ChaosProvider

    wrappers: list[Any] = []

    def wrap(provider, ref):
        w = ChaosProvider(provider, rate, seed=f"{seed}:{ref}")
        wrappers.append(w)
        return w

    inner = gateway._factory  # noqa: SLF001 — test hook for chaos runs
    for ref, provider in list(gateway._providers.items()):  # noqa: SLF001
        gateway._providers[ref] = wrap(provider, ref)  # noqa: SLF001
    gateway._factory = lambda ref: wrap(inner(ref), ref)  # noqa: SLF001
    return wrappers


def _is_test(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path.startswith(("tests/", "test/")) or name.startswith("test_") or ".test." in name


def plan_metrics(task, plan_events: list[dict], changed: set[str]) -> dict[str, Any]:
    """Planning eval (U7) for one run. Empty when no planner ran.

    precision: planned non-test files that the task needed (expected or allowed);
    adherence: changed non-test files that some step planned."""
    first = next((e for e in plan_events if e.get("steps")), None)
    if first is None:
        return {}
    planned = {f.strip("./") for e in plan_events for fs in e.get("files") or [] for f in fs}
    planned_src = {f for f in planned if not _is_test(f)}
    needed = {f.strip("./") for f in set(task.expected_changed) | set(task.allowed_changed)}
    changed_src = {f.strip("./") for f in changed if not _is_test(f)}
    review = first.get("review") or {}
    return {
        "plan_steps": first.get("steps"),
        "plan_precision": round(len(planned_src & needed) / len(planned_src), 3)
        if planned_src
        else None,
        "plan_adherence": round(len(changed_src & planned) / len(changed_src), 3)
        if changed_src
        else None,
        "plan_dependencies": sum(len(d) for d in first.get("dependencies") or []),
        "plan_graph_ok": not first.get("graph_issues"),
        "plan_lint_issues": len(review.get("issues") or []),
        "plan_lint_remaining": len(review.get("remaining") or []),
        "replans": sum(1 for e in plan_events if e.get("replan")),
    }


async def planning_eval_task(
    task, model: str, *, config: AppConfig | None = None, provider=None, home: Path | None = None
) -> dict[str, Any]:
    """Planner-only eval (U7): call the planning role on the task's repository and score the
    plan against the reference file set, with no execution. Cheap: one or two planner calls."""
    from trendlab.agent.tasks import Plan
    from trendlab.app import TrendLabApp
    from trendlab.benchmarks import suite as suite_mod

    config = load_config() if config is None else config
    with tempfile.TemporaryDirectory(prefix=f"trendlab-plan-{task.id}-") as tmp:
        root = Path(tmp) / "repo"
        prompt = suite_mod.materialize(task, root)
        tl = TrendLabApp(
            root,
            config,
            model_ref=model,
            provider=provider,
            console=Console(quiet=True),
            data_dir=home,
        )
        await tl.start(interactive=False)
        try:
            steps = await tl._plan_steps(prompt)  # noqa: SLF001 — the eval targets this call
            review = (tl.agent.plan_review if tl.agent else None) or {}
            cost = tl.costs.total_usd if tl.costs else 0.0
        finally:
            await tl.stop()
    row: dict[str, Any] = {"task": task.id, "tier": task.tier, "model": model, "cost": cost}
    if not steps:
        return {**row, "planned": False}
    from trendlab.agent.planner import apply_steps

    plan = Plan()
    apply_steps(plan, steps)
    files = {f.strip("./") for st in steps for f in st.get("files") or []}
    src = {f for f in files if not _is_test(f)}
    needed = {f.strip("./") for f in set(task.expected_changed) | set(task.allowed_changed)}
    required = {f.strip("./") for f in task.expected_changed}
    return {
        **row,
        "planned": True,
        "steps": len(steps),
        # recall over the files the task requires; precision also credits allowed extras
        "recall": round(len(src & required) / len(required), 3) if required else None,
        "precision": round(len(src & needed) / len(src), 3) if src else None,
        "plans_tests": any(_is_test(f) for f in files)
        or any("test" in st.get("title", "").lower() for st in steps),
        "final_validation": bool(steps[-1].get("validation")),
        "dependencies": sum(len(t.dependencies) for t in plan.tasks),
        "parallel_groups": len(plan.parallel_safe()),
        "graph_ok": not plan.graph_issues(),
        "lint_issues": review.get("issues") or [],
        "lint_remaining": review.get("remaining") or [],
    }


def summarize_planning(rows: list[dict[str, Any]]) -> dict[str, Any]:
    planned = [r for r in rows if r.get("planned")]
    n = len(planned)

    def mean(key):
        vals = [r[key] for r in planned if r.get(key) is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    def share(pred):
        return round(sum(1 for r in planned if pred(r)) / n, 3) if n else None

    return {
        "tasks": len(rows),
        "planned": n,
        "recall": mean("recall"),
        "precision": mean("precision"),
        "plans_tests": share(lambda r: r["plans_tests"]),
        "final_validation": share(lambda r: r["final_validation"]),
        "graph_ok": share(lambda r: r["graph_ok"]),
        "with_dependencies": share(lambda r: r["dependencies"] > 0),
        "lint_flagged": share(lambda r: r["lint_issues"]),
        "lint_clean_after_review": share(lambda r: not r["lint_remaining"]),
        "cost": round(sum(r.get("cost") or 0.0 for r in rows), 4),
    }


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
    # U1: intervals, repeat-run stability and tool reliability
    from trendlab.benchmarks.stats import bootstrap_ci, pass_at_k, wilson_interval

    passes = sum(1 for r in ran if r.get("passes"))
    summary["passes_ci95"] = wilson_interval(passes, len(ran))
    summary["cost_per_task_ci95"] = bootstrap_ci([r.get("cost", 0.0) for r in ran])
    summary.update(pass_at_k(ran))
    calls = sum(r.get("tool_calls", 0) for r in ran)
    fails = sum(r.get("tool_failures", 0) for r in ran)
    summary["tool_calls"] = calls
    summary["tool_success_rate"] = round((calls - fails) / calls, 3) if calls else 1.0
    summary["error_rate"] = round(
        sum(1 for r in ran if r.get("status") != "COMPLETED" or not r.get("passes")) / n, 3
    )
    from trendlab.agent.judge import judge_accuracy

    summary["judge"] = judge_accuracy(ran)  # U2: verifier verdicts vs hidden-test truth
    from trendlab.agent.judge import verifier_scores
    from trendlab.benchmarks.stats import capability_map

    summary["verifier"] = verifier_scores(ran)
    with_ref = [r for r in ran if r.get("ref_similarity") is not None]
    summary["exact_match"] = (
        round(sum(1 for r in with_ref if r.get("exact_match")) / len(with_ref), 3)
        if with_ref
        else None
    )
    summary["ref_similarity"] = (
        round(sum(r["ref_similarity"] for r in with_ref) / len(with_ref), 4) if with_ref else None
    )
    summary["leakage"] = sum(1 for r in ran if r.get("leakage"))
    summary["wall_per_task_ci95"] = bootstrap_ci([r.get("wall_s", 0.0) for r in ran])
    rubric: dict[str, list[int]] = {}
    for r in ran:
        for k, v in (r.get("verifier_rubric") or {}).items():
            rubric.setdefault(k, []).append(v)
    summary["rubric_mean"] = {k: round(sum(v) / len(v), 2) for k, v in rubric.items()}
    adv = [r for r in ran if r.get("tier") == "adversarial"]
    summary["injection_resisted"] = (
        round(sum(1 for r in adv if r.get("safe")) / len(adv), 3) if adv else None
    )
    jb = [r for r in ran if r.get("tier") == "jailbreak"]
    summary["jailbreak_resisted"] = (
        round(sum(1 for r in jb if r.get("safe")) / len(jb), 3) if jb else None
    )
    summary["safe"] = round(sum(1 for r in ran if r.get("safe", True)) / n, 3)
    summary["crashed"] = sum(1 for r in ran if r.get("status") == "CRASHED")
    summary["roi"] = roi(ran)
    summary["load"] = load_profile(ran)
    summary["failures_by_code"] = by_code(
        [r["failure_code"] if "failure_code" in r else classify_row(r) for r in ran]
    )
    summary["read_before_edit"] = round(
        sum(1 for r in ran if r.get("read_before_edit", True)) / n, 3
    )
    summary["validated_after_edit"] = round(
        sum(1 for r in ran if r.get("validated_after_edit", True)) / n, 3
    )
    recalls = [r["plan_recall"] for r in ran if r.get("plan_recall") is not None]
    summary["plan_recall"] = round(sum(recalls) / len(recalls), 3) if recalls else None
    spoken = [r for r in ran if r.get("answer_words") is not None]
    if spoken:
        words = sorted(r["answer_words"] for r in spoken)
        eases = [r["reading_ease"] for r in spoken if r.get("reading_ease") is not None]
        summary["communication"] = {
            "answers": len(spoken),
            "median_words": words[len(words) // 2],
            "reading_ease": round(sum(eases) / len(eases), 1) if eases else None,
            "robospeak_rate": round(sum(1 for r in spoken if r.get("robospeak")) / len(spoken), 3),
            "undefined_acronym_rate": round(
                sum(1 for r in spoken if r.get("undefined_acronyms")) / len(spoken), 3
            ),
        }
    planned = [r for r in ran if r.get("plan_steps")]
    if planned:

        def mean(key):
            vals = [r[key] for r in planned if r.get(key) is not None]
            return round(sum(vals) / len(vals), 3) if vals else None

        summary["planning"] = {
            "plans": len(planned),
            "recall": summary["plan_recall"],
            "precision": mean("plan_precision"),
            "adherence": mean("plan_adherence"),
            "graph_ok": round(sum(1 for r in planned if r.get("plan_graph_ok")) / len(planned), 3),
            "with_dependencies": sum(1 for r in planned if r.get("plan_dependencies")),
            "lint_issue_rate": round(
                sum(1 for r in planned if r.get("plan_lint_issues")) / len(planned), 3
            ),
            "lint_remaining": sum(r.get("plan_lint_remaining") or 0 for r in planned),
            "replans": sum(r.get("replans") or 0 for r in planned),
        }
    summary["chaos_injected"] = sum(r.get("chaos_injected", 0) for r in ran)
    routed = [r for r in ran if r.get("route") and r.get("route_expected")]
    summary["route_accuracy"] = (
        round(sum(1 for r in routed if r["route"] == r["route_expected"]) / len(routed), 3)
        if routed
        else None
    )
    summary["hallucination_rate"] = round(sum(1 for r in ran if r.get("unsupported_claims")) / n, 3)
    summary["capability"] = {
        "by_defect": capability_map(ran, "defect_kind"),
        "by_lang": capability_map(ran, "lang"),
        "by_tier": capability_map(ran, "tier"),
        "symptom_only": capability_map(ran, "symptom_only"),
    }
    return summary


def scorecard(summaries: list[dict[str, Any]], compare: dict[str, Any] | None = None) -> str:
    """Markdown scorecard of one or two configurations (U1 scorecard)."""
    rows = [
        ("tasks", "tasks"),
        ("pass rate", "passes"),
        ("pass rate 95% CI", "passes_ci95"),
        ("pass@k (k)", None),
        ("flaky tasks", "flaky_tasks"),
        ("located file", "located"),
        ("root cause ±3 lines", "root_cause"),
        ("exact match with reference", "exact_match"),
        ("similarity to reference", "ref_similarity"),
        ("no collateral edits", "no_collateral"),
        ("regression test added", "regression_added"),
        ("hallucination rate", "hallucination_rate"),
        ("tool success rate", "tool_success_rate"),
        ("error rate", "error_rate"),
        ("cost (USD)", "cost"),
        ("cost per task 95% CI", "cost_per_task_ci95"),
        ("lead tokens", "tokens_lead"),
        ("wall time (s)", "wall_s"),
    ]
    head = (
        "| metric | "
        + " | ".join(s.get("config", f"config {i + 1}") for i, s in enumerate(summaries))
        + " |"
    )
    lines = [head, "|---|" + "---|" * len(summaries)]
    for label, key in rows:
        if key is None:
            vals = [f"{s.get('pass_at_k')} ({s.get('k')})" for s in summaries]
        else:
            vals = [str(s.get(key, "")) for s in summaries]
        lines.append(f"| {label} | " + " | ".join(vals) + " |")
    for s in summaries:
        v = (s.get("verifier") or {}).get("classifier") or {}
        if v.get("n"):
            lines.append(
                f"\nVerifier ({s.get('config')}): precision {v.get('precision')}, recall "
                f"{v.get('recall')}, F1 {v.get('f1')}, FPR {v.get('false_positive_rate')}, "
                f"FNR {v.get('false_negative_rate')} over {v.get('n')} judged changes."
            )
        weak = [
            k
            for k, d in (s.get("capability") or {}).get("by_defect", {}).items()
            if d["passes"] < 1.0
        ]
        if weak:
            lines.append(f"Weak spots ({s.get('config')}): " + ", ".join(weak))
    if compare:
        pp = compare["paired_passes"]
        g = compare["gate"]
        verdict = "passed" if g["ok"] else "failed: " + "; ".join(g["reasons"])
        lines.append(
            f"\nPaired: second wins {pp['b_wins']}, loses {pp['b_losses']}, ties {pp['ties']}; "
            f"exact sign test p = {pp['p_value']}; cost ratio {compare['cost_ratio']}×; "
            f"gate {verdict}."
        )
    return "\n".join(lines)


def compare_summaries(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Per-metric deltas b − a (§8.4)."""
    deltas = {}
    for k in (*METRICS, "interventions", "cost", "tokens_lead", "wall_s"):
        deltas[k] = round((b.get(k) or 0) - (a.get(k) or 0), 4)
    return deltas


def seeded_diffs(task) -> tuple[str, str]:
    """(bad, good) unified diffs for a suite task: introducing its defect, and fixing it. A
    defect that spans several files (a broken contract) is seeded in all of them, so the
    'good' diff really is the whole fix."""
    import difflib as _difflib

    from trendlab.benchmarks import suite as suite_mod

    base = suite_mod.BASES[task.lang]
    pairs = []
    ref = base[task.defect.file]
    pairs.append((task.defect.file, ref, ref.replace(task.defect.old, task.defect.new, 1)))
    for rel, (old, new) in task.defect.extra.items():
        before = base.get(rel, "")
        after = before.replace(old, new, 1) if old else new
        if after != before:
            pairs.append((rel, before, after))

    def udiff(a, b, rel):
        return f"diff --git a/{rel} b/{rel}\n" + "".join(
            _difflib.unified_diff(
                a.splitlines(True), b.splitlines(True), f"a/{rel}", f"b/{rel}", n=8
            )
        )

    bad = "".join(udiff(ref_, buggy, rel) for rel, ref_, buggy in pairs)
    good = "".join(udiff(buggy, ref_, rel) for rel, ref_, buggy in pairs)
    return bad, good


async def review_eval_task(task, call, *, mode: str = "auto") -> dict[str, Any]:
    """U13 reviewer eval on one task: the defect-introducing diff must draw a high/med model
    finding in the defect file (recall, localisation); the correct fix must draw no high model
    finding (false alarm). Static findings are excluded from both."""
    from trendlab.agent.review import review_diff

    bad, good = seeded_diffs(task)
    rel = task.defect.file
    r_bad = await review_diff(call, bad, intent=f"Small cleanup in {rel}.", mode=mode)
    r_good = await review_diff(call, good, intent=task.defect.symptom, mode=mode)

    def model(fs):
        return [f for f in fs if not f.get("static")]

    # caught = a bug-finding lens names the problem in the right file (a "no test" note from the
    # tests lens is true of any change and does not count)
    hits = [
        f
        for f in model(r_bad["findings"])
        if f["severity"] in {"high", "med"}
        and f["file"].endswith(rel)
        and f["lens"] in {"correctness", "edge_cases", "security", "structure"}
    ]
    near = [f for f in hits if f.get("line") and abs(f["line"] - task.answer_line) <= 3]
    false_alarm = [f for f in model(r_good["findings"]) if f["severity"] == "high"]
    return {
        "task": task.id,
        "kind": task.defect.kind,
        "mode": r_bad["mode"],
        "caught": bool(hits),
        "localised": bool(near),
        "false_alarm": bool(false_alarm),
        "findings_bad": len(model(r_bad["findings"])),
        "findings_good": len(model(r_good["findings"])),
        "noise_good": sum(1 for f in model(r_good["findings"]) if f["severity"] in {"high", "med"}),
        "calls": r_bad["calls"] + r_good["calls"],
        "lenses_catching": sorted({f["lens"] for f in hits}),
    }


async def delegate_eval_task(
    task, model: str, *, config: AppConfig | None = None, provider=None, home: Path | None = None
) -> dict[str, Any]:
    """U15 delegation eval: a read-only explorer sub-agent gets only the bug report and must
    hand back the defect's location with verifiable evidence."""
    import re as _re

    from trendlab.app import TrendLabApp
    from trendlab.benchmarks import suite as suite_mod
    from trendlab.orchestration.subagents import SubAgentTask

    config = load_config() if config is None else config
    with tempfile.TemporaryDirectory(prefix=f"trendlab-suite-deleg-{task.id}-") as tmp:
        root = Path(tmp) / "repo"
        suite_mod.materialize(task, root)
        tl = TrendLabApp(
            root,
            config,
            model_ref=model,
            provider=provider,
            console=Console(quiet=True),
            data_dir=home,
        )
        await tl.start(interactive=False)
        try:
            report = await tl.subagents.run(
                SubAgentTask(
                    role="explorer",
                    objective="Bug report: " + task.defect.symptom + " Find the code that causes "
                    "it. Do not fix it. Cite the exact path:line of the cause in EVIDENCE.",
                )
            )
        finally:
            await tl.stop()
    text = report.findings or ""
    rel = task.defect.file
    refs = [(p, int(n)) for p, n in _re.findall(r"([\w./-]+\.\w+):(\d+)", text)]
    located = any(p.endswith(rel) for p, _ in refs) or rel in text
    line_hit = any(p.endswith(rel) and abs(n - task.answer_line) <= 3 for p, n in refs)
    return {
        "task": task.id,
        "kind": task.defect.kind,
        "status": report.status,
        "located": located,
        "line_hit": line_hit,
        "handoff_complete": report.handoff.get("complete"),
        "evidence_cited": report.handoff.get("evidence_cited"),
        "evidence_verified": report.handoff.get("evidence_verified"),
        "cost": round(report.cost_usd, 4),
        "tool_calls": report.tool_calls,
        "elapsed_s": round(report.elapsed_s, 1),
    }


def summarize_delegate_eval(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if not n:
        return {"tasks": 0}
    cited = sum(r.get("evidence_cited") or 0 for r in rows)
    verified = sum(r.get("evidence_verified") or 0 for r in rows)
    return {
        "tasks": n,
        "located": round(sum(1 for r in rows if r["located"]) / n, 3),
        "line_hit": round(sum(1 for r in rows if r["line_hit"]) / n, 3),
        "handoff_complete": round(sum(1 for r in rows if r["handoff_complete"]) / n, 3),
        "evidence_verified_share": round(verified / cited, 3) if cited else None,
        "cost": round(sum(r["cost"] for r in rows), 4),
        "mean_tool_calls": round(sum(r["tool_calls"] for r in rows) / n, 1),
    }


async def critique_eval(reviewers: dict[str, Any], *, adversary=None) -> dict[str, Any]:
    """U15 panel eval: recall of planted design flaws for each reviewer alone and for the panel
    (union), plus the false-consensus check: how many consensus points match no planted flaw."""
    from trendlab.agent.critique import critique
    from trendlab.benchmarks.designs import DESIGNS, found

    rows = []
    for d in DESIGNS:
        res = await critique(d["doc"], reviewers, adversary=adversary)
        row = {"design": d["id"], "points": len(res["points"]), "consensus": res["consensus"]}
        for name, pts in res["raw"].items():
            row[f"found_{name}"] = sum(1 for pat in d["flaws"].values() if found(pts, pat))
        row["found_panel"] = sum(1 for pat in d["flaws"].values() if found(res["points"], pat))
        row["flaws"] = len(d["flaws"])
        rows.append(row)
    total = sum(r["flaws"] for r in rows)
    names = [k[6:] for k in rows[0] if k.startswith("found_")]
    return {
        "designs": len(rows),
        "flaws": total,
        "recall": {n: round(sum(r[f"found_{n}"] for r in rows) / total, 3) for n in names},
        "rows": rows,
    }


def summarize_review_eval(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from trendlab.benchmarks.stats import wilson_interval

    n = len(rows)
    if not n:
        return {"tasks": 0}
    caught = sum(1 for r in rows if r["caught"])
    fa = sum(1 for r in rows if r["false_alarm"])
    lens_counts: dict[str, int] = {}
    for r in rows:
        for lens in r["lenses_catching"]:
            lens_counts[lens] = lens_counts.get(lens, 0) + 1
    return {
        "tasks": n,
        "recall": round(caught / n, 3),
        "recall_ci95": wilson_interval(caught, n),
        "localised": round(sum(1 for r in rows if r["localised"]) / n, 3),
        "false_alarm_rate": round(fa / n, 3),
        "false_alarm_ci95": wilson_interval(fa, n),
        "calls": sum(r["calls"] for r in rows),
        "noise_per_correct_fix": round(sum(r.get("noise_good", 0) for r in rows) / n, 2),
        "lens_catches": dict(sorted(lens_counts.items(), key=lambda kv: -kv[1])),
    }


def load_profile(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Stress/load view (U11): latency percentiles, makespan and throughput of a run batch."""
    walls = sorted(float(r["wall_s"]) for r in rows if r.get("wall_s") is not None)
    if not walls:
        return None

    def pct(q):
        return round(walls[min(len(walls) - 1, int(q * len(walls)))], 1)

    starts = [r["started_at"] for r in rows if r.get("started_at")]
    ends = [r["ended_at"] for r in rows if r.get("ended_at")]
    span = (max(ends) - min(starts)) if starts and ends else None
    return {
        "tasks": len(walls),
        "wall_p50": pct(0.5),
        "wall_p95": pct(0.95),
        "wall_max": round(walls[-1], 1),
        "makespan_s": round(span, 1) if span else None,
        "tasks_per_min": round(len(walls) / span * 60, 2) if span else None,
        "crashed": sum(1 for r in rows if r.get("status") == "CRASHED"),
        "provider_errors": sum(
            1 for r in rows if str(r.get("failure_code") or "").startswith("PROVIDER")
        ),
    }


def roi(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Outcome per dollar (U10): passes and passes-with-a-regression-test per USD."""
    ran = [r for r in rows if not r.get("skipped")]
    cost = sum(float(r.get("cost") or 0.0) for r in ran)
    passes = sum(1 for r in ran if r.get("passes"))
    tested = sum(1 for r in ran if r.get("passes") and r.get("regression_added"))
    return {
        "cost": round(cost, 4),
        "passes": passes,
        "cost_per_pass": round(cost / passes, 4) if passes else None,
        "passes_per_usd": round(passes / cost, 1) if cost else None,
        "tested_passes_per_usd": round(tested / cost, 1) if cost else None,
    }


def _roi_delta(a_rows: list[dict[str, Any]], b_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """What the extra spend of B over A bought: dollars per extra pass / extra tested pass."""
    ra, rb = roi(a_rows), roi(b_rows)
    extra_cost = rb["cost"] - ra["cost"]

    def per(extra):
        if extra > 0:
            return round(extra_cost / extra, 4)
        return None  # nothing extra bought (or fewer): the spend is not justified by outcome

    tested_a = sum(1 for r in a_rows if r.get("passes") and r.get("regression_added"))
    tested_b = sum(1 for r in b_rows if r.get("passes") and r.get("regression_added"))
    return {
        "a": ra,
        "b": rb,
        "extra_cost": round(extra_cost, 4),
        "usd_per_extra_pass": per(rb["passes"] - ra["passes"]),
        "usd_per_extra_tested_pass": per(tested_b - tested_a),
    }


def compare_rows(
    a_rows: list[dict[str, Any]], b_rows: list[dict[str, Any]], *, max_cost_ratio: float = 1.5
) -> dict[str, Any]:
    """Paired comparison with a significance test and a release gate (U1)."""
    from trendlab.benchmarks.stats import gate, paired_outcomes

    sa, sb = summarize(a_rows), summarize(b_rows)
    paired = paired_outcomes(a_rows, b_rows, "passes")
    ratio = (sb["cost"] / sa["cost"]) if sa["cost"] else float("inf") if sb["cost"] else 1.0
    return {
        "deltas": compare_summaries(sa, sb),
        "paired_passes": paired,
        "cost_ratio": round(ratio, 3),
        "roi": _roi_delta(a_rows, b_rows),
        "gate": gate(paired, ratio, max_cost_ratio=max_cost_ratio),
        "a": sa,
        "b": sb,
    }


CANARY = "py01,py03,py05,py09,py13,py16,ts01,ts03,hd03,hd05"


def append_bench_log(path: Path, title: str, rows: list[dict[str, Any]], note: str = "") -> None:
    """docs/BENCH_LOG.md: one dated block per run, newest last (§8.4)."""
    from datetime import UTC, datetime

    lines = [f"\n## {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC — {title}\n"]
    if note:
        lines.append(note + "\n")
    bulky = {"cost_by_phase", "diff", "prompt", "tools", "unsupported_claims", "guards_fired"}
    keys = [k for k in rows[0] if k not in bulky] if rows else []
    if keys:
        lines.append("| " + " | ".join(keys) + " |")
        lines.append("|" + "---|" * len(keys))
        for r in rows:
            lines.append("| " + " | ".join(str(r.get(k, "")) for k in keys) + " |")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def verify_suite_tasks() -> list[str]:
    """Golden-dataset check: every task materialises, its visible suite behaves as declared,
    its hidden test fails on the defective code, and the base repos are green."""
    import shutil as _shutil
    import tempfile as _tempfile

    from trendlab.benchmarks import suite as suite_mod

    problems: list[str] = []
    for lang, base in suite_mod.BASES.items():
        if not suite_mod.toolchain_available(lang):
            continue
        with _tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "base"
            for rel, content in base.items():
                f = root / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(content)
            if subprocess.run(
                suite_mod.TEST_COMMANDS[lang], shell=True, cwd=root, capture_output=True
            ).returncode:
                problems.append(f"base repo for {lang} is not green")
    for t in suite_mod.TASKS:
        if not suite_mod.toolchain_available(t.lang):
            continue
        with _tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "r"
            try:
                suite_mod.materialize(t, root)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{t.id}: does not materialise ({exc})")
                continue
            if suite_mod.check_forbid(t, root, []):
                problems.append(f"{t.id}: safety checks fail before any run")
            if t.answer_keywords:
                continue
            vis = subprocess.run(
                t.test_command, shell=True, cwd=root, capture_output=True
            ).returncode
            if (vis != 0) != t.defect.visible_fail:
                problems.append(
                    f"{t.id}: visible suite {'fails' if vis else 'passes'} "
                    "but the task says otherwise"
                )
            if suite_mod.write_hidden_test(t, root) is not None:
                if (
                    subprocess.run(
                        t.test_command, shell=True, cwd=root, capture_output=True
                    ).returncode
                    == 0
                ):
                    problems.append(f"{t.id}: hidden test passes on the defective code")
            _shutil.rmtree(root, ignore_errors=True)
    return problems
