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

        def count(e):
            nonlocal tool_calls, interventions
            if e.type == EventType.TOOL_STARTED:
                tool_calls += 1
            if e.type == EventType.APPROVAL_REQUESTED:
                interventions += 1

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
