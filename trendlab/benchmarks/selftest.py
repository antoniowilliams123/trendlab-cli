"""``trendlab selftest`` (uplift U31): an offline end-to-end check of the harness, $0.

Runs in a temporary TRENDLAB_HOME and a materialised suite repository, with scripted model
responses, through the real code paths: an agent run that fixes a bug (graded by the hidden
test), deterministic replay of that run, a review saved to the ledger, the architecture,
health and mutation tools, and the guards that must catch a seeded hallucination and a seeded
test-gaming diff. Each stage reports ok / failed with its time.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


async def run() -> list[dict[str, Any]]:
    from rich.console import Console

    from trendlab.agent.arch import report as arch_report
    from trendlab.agent.mutation import run as mutate
    from trendlab.agent.review import review_diff, save_review
    from trendlab.agent.scope import test_gaming
    from trendlab.agent.symbols import unresolved
    from trendlab.app import TrendLabApp
    from trendlab.benchmarks import suite as suite_mod
    from trendlab.benchmarks.cassette import deterministic_replay
    from trendlab.config.schema import AppConfig, PermissionMode, ProviderConfig
    from trendlab.engine.health import snapshot
    from trendlab.providers.base import ModelResponse, ToolCall
    from trendlab.providers.scripted import ScriptedProvider
    from trendlab.sessions.store import SessionStore

    results: list[dict[str, Any]] = []

    async def stage(name, fn):
        t = time.monotonic()
        try:
            detail = await fn() if asyncio.iscoroutinefunction(fn) else fn()
            results.append(
                {
                    "stage": name,
                    "ok": bool(detail is not False),
                    "detail": detail,
                    "seconds": round(time.monotonic() - t, 2),
                }
            )
        except Exception as exc:  # noqa: BLE001 — a self-test reports, never crashes
            results.append(
                {
                    "stage": name,
                    "ok": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                    "seconds": round(time.monotonic() - t, 2),
                }
            )

    old_home = os.environ.get("TRENDLAB_HOME")
    with tempfile.TemporaryDirectory(prefix="trendlab-selftest-") as tmp:
        os.environ["TRENDLAB_HOME"] = str(Path(tmp) / "home")
        try:
            task = suite_mod.get_task("py01-off_by_one")
            root = Path(tmp) / "repo"
            prompt = suite_mod.materialize(task, root)
            for args in (
                ["init", "-q"],
                ["add", "-A"],
                ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"],
            ):
                subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
            cfg = AppConfig(providers={"scripted": ProviderConfig()})
            cfg.remote_approval.enabled = False
            cfg.planner.enabled = False

            def call(i, name, **a):
                return ModelResponse(tool_calls=[ToolCall(id=str(i), name=name, arguments=a)])

            script = [
                call(1, "read_file", path="shop/util.py"),
                call(
                    2,
                    "patch_file",
                    path="shop/util.py",
                    old_text="len(items) - 1, size",
                    new_text="len(items), size",
                ),
                call(
                    3,
                    "write_file",
                    path="tests/test_regress.py",
                    content="from shop.util import chunks\n\n\ndef test_tail():\n"
                    "    assert chunks([1, 2, 3], 2) == [[1, 2], [3]]\n",
                ),
                call(4, "run_tests", kind="test"),
                ModelResponse(text="Fixed the off-by-one in chunks(); tests pass."),
            ]
            state: dict[str, Any] = {}

            async def agent_run():
                tl = TrendLabApp(
                    root,
                    cfg,
                    provider=ScriptedProvider(list(script)),
                    model_ref="scripted:m",
                    permission_mode=PermissionMode.UNSAFE,
                    console=Console(quiet=True),
                )
                await tl.start(interactive=False)
                try:
                    res = await tl.run_prompt(prompt)
                    state["session"] = tl.session_id
                finally:
                    await tl.stop()
                suite_mod.write_hidden_test(task, root)
                ok = (
                    subprocess.run(
                        task.test_command, shell=True, cwd=root, capture_output=True
                    ).returncode
                    == 0
                )
                return f"{res.status}, hidden test {'passes' if ok else 'FAILS'}" if ok else False

            async def replay():
                store = SessionStore(Path(os.environ["TRENDLAB_HOME"]) / "sessions.db")
                try:
                    rep = await deterministic_replay(store, state["session"], config=cfg)
                finally:
                    store.close()
                return (
                    f"{rep['model_calls_served']} calls replayed" if rep.get("identical") else False
                )

            async def review():
                diff = subprocess.run(
                    ["git", "-C", str(root), "diff"], capture_output=True, text=True
                ).stdout

                async def reviewer(messages):
                    if "REAL defect" in messages[0]["content"]:
                        return '{"keep": [], "drop": []}'
                    return '{"findings": []}'

                res = await review_diff(reviewer, diff, intent=prompt)
                saved = save_review(root, {"source": "selftest", **res})
                return f"review {saved['id']} saved, {len(res['findings'])} findings"

            def arch():
                r = arch_report(root)
                return (
                    f"{r['modules']} modules, {len(r['cycles'])} cycles"
                    if not r["cycles"]
                    else False
                )

            def health():
                s = snapshot(root)
                return f"test ratio {s['test_ratio']}, {s['functions']} functions"

            def mutation():
                r = mutate(root, "shop/util.py", task.test_command, limit=6)
                return f"{r['killed']}/{r['mutants']} mutants killed"

            def symbols():
                (root / "shop/report.py").write_text(
                    (root / "shop/report.py").read_text() + "\nfrom shop.util import chunk_list\n"
                )
                found = unresolved(root, "shop/report.py")
                subprocess.run(
                    ["git", "-C", str(root), "checkout", "--", "shop/report.py"],
                    check=True,
                    capture_output=True,
                )
                return (
                    "hallucinated import caught" if any("chunk_list" in f for f in found) else False
                )

            def gaming():
                found = test_gaming(
                    {"tests/test_x.py": ["-    assert total == 90\n+    pass"]}, "fix the total"
                )
                return "weakened test caught" if found else False

            for name, fn in (
                ("agent run", agent_run),
                ("deterministic replay", replay),
                ("review ledger", review),
                ("architecture", arch),
                ("health", health),
                ("mutation", mutation),
                ("symbol check", symbols),
                ("test-gaming check", gaming),
            ):
                await stage(name, fn)
        finally:
            if old_home is None:
                os.environ.pop("TRENDLAB_HOME", None)
            else:
                os.environ["TRENDLAB_HOME"] = old_home
    return results
