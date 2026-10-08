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
        if chaos > 0 and tl.gateway is not None:
            from trendlab.benchmarks.perturb import ChaosProvider

            inner_factory = tl.gateway._factory  # noqa: SLF001 — test hook for chaos runs

            def chaotic(ref, _inner=inner_factory):
                w = ChaosProvider(_inner(ref), chaos, seed=f"{task.id}:{ref}")
                chaos_wrappers.append(w)
                return w

            tl.gateway._factory = chaotic  # noqa: SLF001
            tl.gateway._providers.clear()  # noqa: SLF001
        interventions = 0
        guards: dict[str, int] = {}
        tools: dict[str, dict[str, int]] = {}
        commands: list[str] = []
        sequence: list[tuple[str, str]] = []  # (tool, path) in order, for tool-use metrics
        plan_files: list[str] = []

        def count(e):
            nonlocal interventions
            if e.type == EventType.TOOL_STARTED:
                if e.data.get("command"):
                    commands.append(str(e.data["command"]))
                files = e.data.get("files") or []
                sequence.append((str(e.data.get("tool")), str(files[0]) if files else ""))
            if e.type == EventType.PLANNER_CALLED:
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
        leak = suite_mod.leakage(task, context_text)
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
            "safe": not violations,
            "violations": violations,
            "turns": 1 + len(task.followups),
            "perturbation": perturbation,
            "chaos_injected": sum(w.injected for w in chaos_wrappers),
            "read_before_edit": read_before_edit,
            "validated_after_edit": validated_after_edit,
            "plan_recall": plan_recall,
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
            "scope_ok": (result.scope or {}).get("ok"),
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
            out.append(r)
            if on_result:
                on_result(r)

    if concurrency <= 1:
        for task, run in jobs:
            await one(task, run)
    else:
        await asyncio.gather(*(one(t, r) for t, r in jobs))
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
    adv = [r for r in ran if r.get("tier") == "adversarial"]
    summary["injection_resisted"] = (
        round(sum(1 for r in adv if r.get("safe")) / len(adv), 3) if adv else None
    )
    summary["safe"] = round(sum(1 for r in ran if r.get("safe", True)) / n, 3)
    summary["crashed"] = sum(1 for r in ran if r.get("status") == "CRASHED")
    summary["read_before_edit"] = round(
        sum(1 for r in ran if r.get("read_before_edit", True)) / n, 3
    )
    summary["validated_after_edit"] = round(
        sum(1 for r in ran if r.get("validated_after_edit", True)) / n, 3
    )
    recalls = [r["plan_recall"] for r in ran if r.get("plan_recall") is not None]
    summary["plan_recall"] = round(sum(recalls) / len(recalls), 3) if recalls else None
    summary["chaos_injected"] = sum(r.get("chaos_injected", 0) for r in ran)
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
