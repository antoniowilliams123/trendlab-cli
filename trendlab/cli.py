"""Top-level ``trendlab`` command."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from trendlab import PRODUCT_NAME, __version__
from trendlab.config.loader import (
    ConfigError,
    global_config_path,
    load_config,
    trendlab_home,
    update_global_config,
)
from trendlab.config.schema import PermissionMode

app = typer.Typer(
    name="trendlab",
    help=f"{PRODUCT_NAME} — a local-first, model-agnostic agentic coding harness.",
    add_completion=False,
    no_args_is_help=False,
    invoke_without_command=True,
)
remote_app = typer.Typer(help="Remote (phone) approval settings.")
app.add_typer(remote_app, name="remote")
secret_app = typer.Typer(help="Secrets stored outside config (~/.trendlab/secrets, mode 0600).")
app.add_typer(secret_app, name="secret")
from trendlab.ui.theme import make_console  # noqa: E402

console = make_console()


def _version(value: bool) -> None:
    if value:
        typer.echo(f"{PRODUCT_NAME} {__version__}")
        raise typer.Exit()


def unsafe_banner(allow_destructive: bool) -> str:
    kept = "elevated commands (sudo) and paths outside the project stay off-limits"
    irreversible = (
        "irreversible commands (rm -rf, git reset --hard, force-push) run without asking"
        if allow_destructive
        else "irreversible commands still ask at the terminal"
    )
    return (
        f"[bold black on #ffd21f] AUTO MODE [/bold black on #ffd21f] [#ffd21f]no approval prompts "
        f"this session: edits, shell, installs, network and deletes run without asking; "
        f"{irreversible}; "
        f"{kept}. Every auto-approved operation is logged; /undo restores the pre-edit checkpoint. "
        f'Turn prompts on with --safe, /mode ask, or permission_mode = "ask" in config.[/#ffd21f]'
    )


def _first_run_check(config, model_ref: str) -> None:
    """Spec §56: a useful message instead of a stack trace when nothing is configured."""

    from trendlab.providers.registry import parse_model_ref

    try:
        provider, _ = parse_model_ref(model_ref)
    except Exception:  # noqa: BLE001
        return
    pcfg = config.providers.get(provider)
    if pcfg is None:
        console.print(
            f"[yellow]Provider {provider!r} is not configured in {global_config_path()}.[/yellow]"
        )
        console.print(
            'Add e.g.\n  [providers.deepseek]\n  type = "openai_compatible"\n'
            '  base_url = "https://api.deepseek.com/v1"\n  api_key_env = "DEEPSEEK_API_KEY"'
        )
        return
    from trendlab.security.secrets import resolve_secret

    if pcfg.type != "ollama" and pcfg.api_key_env and not resolve_secret(pcfg.api_key_env):
        console.print(
            f"[yellow]Welcome to {PRODUCT_NAME}.[/yellow] No key found for provider {provider!r}: "
            f"run [bold]trendlab secret set {pcfg.api_key_env}[/bold] or export it in your "
            f"environment (secrets are never stored in config)."
        )


@app.callback()
def main_callback(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", callback=_version, is_eager=True, help="Show version and exit."
    ),
    model: str | None = typer.Option(None, "--model", "-m", help="provider:model to use."),
    project: Path = typer.Option(Path.cwd(), "--project", "-C", help="Project directory."),
    prompt: str | None = typer.Option(
        None, "--prompt", "-p", help="Run one prompt non-interactively."
    ),
    mode: PermissionMode | None = typer.Option(None, "--mode", help="Permission mode."),
    auto_edit: bool = typer.Option(False, "--auto-edit", help="Shortcut for --mode auto_edit."),
    resume: str | None = typer.Option(None, "--resume", help="Resume a session id, or 'latest'."),
    output: str = typer.Option("text", "--output", help="text | json (with -p)."),
    max_cost: float | None = typer.Option(None, "--max-cost", help="Hard USD budget for this run."),
    plain: bool = typer.Option(
        False, "--plain", help="Use the plain REPL instead of the full-screen TUI."
    ),
    dangerously_skip_permissions: bool = typer.Option(
        False,
        "--auto",
        "--dangerously-skip-permissions",
        help="AUTO mode (the default): run edits, shell, installs, network and deletes without "
        "asking. Elevated commands and paths outside the project stay off-limits; irreversible "
        "commands still ask unless --allow-irreversible. --safe turns prompts on.",
    ),
    safe: bool = typer.Option(
        False, "--safe", help="Shortcut for --mode ask (approval prompts on)."
    ),
    worktree: str | None = typer.Option(
        None, "--worktree", help="Create a throwaway git worktree with this name and work there."
    ),
    allow_path: list[str] = typer.Option(
        [],
        "--allow-path",
        help="Change scope for this session: edits outside these globs are refused (repeatable).",
    ),
    tools_allow: str | None = typer.Option(
        None, "--tools", help="Comma-separated tool allowlist for this session."
    ),
    telegram: bool = typer.Option(
        False, "--telegram", help="Remote control from your Telegram chat for this session."
    ),
    plan_gate: bool = typer.Option(
        False,
        "--plan-gate",
        help="Ask for a go-ahead (terminal, phone page or Telegram buttons) before the first "
        "change of every run.",
    ),
    allow_destructive: bool = typer.Option(
        False,
        "--allow-irreversible",
        "--allow-destructive",
        help="In auto mode: also run rm -rf / history-rewriting git without asking.",
    ),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    try:
        config = load_config(project)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    if max_cost is not None:
        config.limits.max_cost_usd = max_cost
    if allow_path:
        config.governance.change_allow = list(allow_path)
    if tools_allow:
        config.tools.allow = [t.strip() for t in tools_allow.split(",") if t.strip()]
    if auto_edit:
        mode = PermissionMode.AUTO_EDIT
    if safe:
        mode = PermissionMode.ASK
    if dangerously_skip_permissions:
        mode = PermissionMode.UNSAFE
    effective = PermissionMode(mode or config.defaults.permission_mode)
    allow_destructive = allow_destructive or config.defaults.allow_destructive
    if plan_gate:
        config.plan_gate.enabled = True
    if telegram:
        config.telegram_bridge.enabled = True
    if allow_destructive and effective != PermissionMode.UNSAFE:
        console.print(
            "[red]--allow-irreversible only applies in auto mode "
            "(the default, or --dangerously-skip-permissions)[/red]"
        )
        raise typer.Exit(2)
    if effective == PermissionMode.UNSAFE and output != "json":
        console.print(unsafe_banner(allow_destructive))
    from trendlab.app import TrendLabApp

    if worktree:
        from trendlab.orchestration.gitflow import WorktreeManager

        try:
            project = WorktreeManager(project.resolve()).create_sync(worktree)
        except RuntimeError as exc:
            console.print(f"[danger]{exc}[/danger]")
            raise typer.Exit(2) from exc
        console.print(f"[ok]working in worktree[/ok] {project}")
    if prompt:
        _first_run_check(config, model or config.defaults.model)
        tl_app = TrendLabApp(
            project,
            config,
            model_ref=model,
            console=console,
            permission_mode=mode,
            resume=resume,
            allow_destructive=allow_destructive,
        )
        code = asyncio.run(_run_once(tl_app, prompt, output))
        raise typer.Exit(code)
    _first_run_check(config, model or config.defaults.model)
    use_tui = not plain and sys.stdin.isatty() and sys.stdout.isatty()
    if use_tui:
        try:
            from trendlab.ui.tui import run_tui
        except ImportError:
            use_tui = False
    if use_tui:
        run_tui(
            project,
            config,
            model_ref=model,
            permission_mode=mode,
            resume=resume,
            allow_destructive=allow_destructive,
        )
        return
    tl_app = TrendLabApp(
        project,
        config,
        model_ref=model,
        console=console,
        permission_mode=mode,
        resume=resume,
        on_token=lambda t: console.print(t, end="", highlight=False),
        on_thinking=lambda t: console.print(t, end="", style="dim italic", highlight=False),
        allow_destructive=allow_destructive,
    )
    from trendlab.ui.repl import Repl

    repl = Repl(tl_app)
    asyncio.run(_run_repl(repl))


async def _run_repl(repl) -> None:
    """Ctrl+C: first press cancels the running task; a second press within 2 s (or a press
    while idle) exits."""
    import time

    loop = asyncio.get_running_loop()
    last = {"t": 0.0}

    def on_sigint() -> None:
        now = time.monotonic()
        if repl.cancel_current() and now - last["t"] > 2.0:
            console.print(
                "\n[warning]Canceling current task… (Ctrl+C again within 2 s to quit)[/warning]"
            )
            last["t"] = now
            return
        console.print("\n[dim]bye[/dim]")
        repl._quit()  # noqa: SLF001
        for task in asyncio.all_tasks(loop):
            if task is not asyncio.current_task(loop):
                task.cancel()

    try:
        loop.add_signal_handler(signal.SIGINT, on_sigint)
    except (NotImplementedError, RuntimeError):
        pass
    try:
        await repl.run()
    except asyncio.CancelledError:
        pass


async def _run_once(tl_app, prompt: str, output: str) -> int:
    quiet = output == "json"
    if quiet:
        tl_app.console = Console(stderr=True, quiet=True)
    await tl_app.start(interactive=not quiet)
    try:
        result = await tl_app.run_prompt(prompt)
    finally:
        await tl_app.stop()
    if quiet:
        typer.echo(json.dumps(result.to_json(), indent=2))
    else:
        console.print(result.report)
    return 0 if result.status == "COMPLETED" else 1


# -- trendlab remote ... ----------------------------------------------------------------
@remote_app.callback(invoke_without_command=True)
def remote_root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        remote_status()


@remote_app.command("status")
def remote_status() -> None:
    cfg = load_config().remote_approval
    notif = load_config().notifications
    t = Table.grid(padding=(0, 2))
    t.add_row("Config file", str(global_config_path()))
    t.add_row("Remote approval", "[green]enabled[/green]" if cfg.enabled else "[dim]disabled[/dim]")
    t.add_row("Bind", f"{cfg.host}:{cfg.port}" + (" (TLS)" if cfg.tls_cert else ""))
    t.add_row("Public URL", cfg.public_url or "-")
    t.add_row("Request timeout", f"{cfg.request_timeout_minutes} min")
    t.add_row("High-risk via phone", "yes" if cfg.allow_high_risk else "no")
    t.add_row("Notifications", notif.provider if notif.enabled else "disabled")
    console.print(t)


@remote_app.command("enable")
def remote_enable(
    host: str = typer.Option(
        "127.0.0.1", help="Address to bind (e.g. your Tailscale IP or 0.0.0.0)."
    ),
    port: int = typer.Option(8787),
    public_url: str | None = typer.Option(None, help="URL your phone will use, if different."),
    timeout: int = typer.Option(30, help="Minutes before an approval request expires."),
    allow_insecure_http: bool = typer.Option(
        False, help="Allow plain HTTP on a non-loopback bind."
    ),
    machine_name: str | None = typer.Option(None, help="Name shown in notifications."),
) -> None:
    values = {
        "enabled": True,
        "host": host,
        "port": port,
        "request_timeout_minutes": timeout,
        "allow_insecure_http": allow_insecure_http,
    }
    if public_url:
        values["public_url"] = public_url
    if machine_name:
        values["machine_name"] = machine_name
    try:
        path = update_global_config("remote_approval", values)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    console.print(f"[green]Remote approval enabled[/green] in {path}")
    remote_url()


@remote_app.command("disable")
def remote_disable() -> None:
    path = update_global_config("remote_approval", {"enabled": False})
    console.print(f"[yellow]Remote approval disabled[/yellow] in {path}")


@remote_app.command("url")
def remote_url() -> None:
    """Print the one-time pairing link to open on your phone."""
    from trendlab.approvals.tokens import load_or_create_token, pairing_url

    cfg = load_config().remote_approval
    scheme = "https" if cfg.tls_cert and cfg.tls_key else "http"
    base = cfg.public_url or f"{scheme}://{cfg.host}:{cfg.port}"
    console.print("Open this on your phone once (it contains your access token — keep it private):")
    console.print(f"[bold]{pairing_url(base, load_or_create_token())}[/bold]")


@remote_app.command("rotate-token")
def remote_rotate_token() -> None:
    """Invalidate all paired phones and print a fresh pairing link."""
    from trendlab.approvals.tokens import rotate_token

    rotate_token()
    console.print("[green]Token rotated.[/green]")
    remote_url()


@app.command("approvals")
def approvals_cmd(limit: int = typer.Option(20, help="Rows to show.")) -> None:
    """Show recent approval decisions across sessions (audit view)."""
    from trendlab.sessions.store import SessionStore

    store = SessionStore(trendlab_home() / "sessions.db")
    rows = store.approvals(limit=limit)
    t = Table(title="Recent approvals")
    for col in ("id", "created", "status", "risk", "action", "via", "machine"):
        t.add_column(col)
    for r in rows:
        t.add_row(
            r["id"][:8],
            r["created_at"][:19],
            r["status"],
            r["risk"],
            r["summary"],
            r["decided_via"] or "-",
            r["machine"],
        )
    console.print(t)
    store.close()


@app.command("sessions")
def sessions_cmd(
    project: Path = typer.Option(Path.cwd(), "--project", "-C"),
    all_projects: bool = typer.Option(False, "--all", help="Across all projects."),
) -> None:
    """List saved sessions; resume one with `trendlab --resume <id>`."""
    from trendlab.sessions.store import SessionStore

    store = SessionStore(trendlab_home() / "sessions.db")
    rows = store.sessions(None if all_projects else str(project.resolve()))
    t = Table(title="Sessions")
    for col in ("id", "updated", "status", "model", "project", "messages", "cost"):
        t.add_column(col)
    for r in rows:
        u = store.usage(r["id"])
        t.add_row(
            r["id"],
            r["updated_at"][:19],
            r["status"],
            r["model"] or "-",
            Path(r["project_path"]).name,
            str(len(store.messages(r["id"]))),
            f"${u['cost_usd']:.3f}",
        )
    console.print(t)
    store.close()


@app.command("bench")
def bench_cmd(
    model: str = typer.Option(..., "--model", "-m", help="provider:model to benchmark."),
    fixture: str | None = typer.Option(None, help="Run one fixture (A..E) instead of all."),
    output: str = typer.Option("text", help="text | json"),
    suite: bool = typer.Option(False, "--suite", help="Run the 50-task suite instead of A–E."),
    tasks: str | None = typer.Option(
        None, "--tasks", help="Suite: count (e.g. 10) or ids/prefixes."
    ),
    lang: str | None = typer.Option(None, "--lang", help="Suite: python | typescript | go."),
    tier: str = typer.Option("base", "--tier", help="Suite: base | hard | all."),
    runs: int = typer.Option(
        1, "--runs", help="Repeat every task N times (pass@k, flakiness, intervals)."
    ),
    gate: bool = typer.Option(
        False,
        "--gate",
        help="With --compare: exit 1 when the second config is significantly worse "
        "or over the cost limit.",
    ),
    canary: bool = typer.Option(False, "--canary", help="Run the fixed 10-task canary set."),
    holdout: bool = typer.Option(
        False, "--holdout", help="Only the holdout tasks (release gate; never tune on these)."
    ),
    scorecard_path: Path | None = typer.Option(
        None, "--scorecard", help="Write a markdown scorecard of the suite run to this file."
    ),
    perturb: str | None = typer.Option(
        None, "--perturb", help="Rewrite prompts: typos | terse | verbose (robustness eval)."
    ),
    chaos: float = typer.Option(0.0, "--chaos", help="Share of model calls that fail transiently."),
    concurrency: int = typer.Option(1, "--concurrency", help="Tasks run at once (stress test)."),
    save_rows: Path | None = typer.Option(
        None, "--save-rows", help="Write every task row (with diffs) as JSON for label/judge-bias."
    ),
    profile: str = typer.Option("harness", "--profile", help="harness | bare (suite)."),
    compare: list[str] | None = typer.Option(
        None,
        "--compare",
        help="Two model@profile values (e.g. deepseek:deepseek-flash@harness and "
        "anthropic:claude-sonnet-5@bare); the suite runs twice and deltas are printed.",
    ),
    sandbox: str | None = typer.Option(None, "--sandbox", help="off | on | docker (suite)."),
    log: bool = typer.Option(
        True, "--log/--no-log", help="Append suite results to docs/BENCH_LOG.md."
    ),
    ablation: bool = typer.Option(
        False, "--ablation", help="Run the harness and each one-component-off variant."
    ),
    sweep: str | None = typer.Option(
        None, "--sweep", help="Sensitivity sweep, e.g. verification.min_diff_lines=0,30,80."
    ),
    counterfactual: Path | None = typer.Option(
        None, "--counterfactual", help="Rows file: rerun only its failed tasks under --profile."
    ),
    verify_suite: bool = typer.Option(
        False, "--verify-suite", help="Check every suite task is well-formed, then exit."
    ),
    planning_eval: bool = typer.Option(
        False,
        "--planning-eval",
        help="Planner only: plan each selected task (no execution) and score the plans.",
    ),
    review_eval: bool = typer.Option(
        False,
        "--review-eval",
        help="Reviewer only: seeded-defect and correct-fix diffs per task; recall, false alarms.",
    ),
    review_mode: str = typer.Option("auto", "--review-mode", help="auto | quick | deep"),
    no_confirm: bool = typer.Option(
        False, "--no-confirm", help="Review eval: skip the confirmation pass (ablation)."
    ),
    delegate_eval: bool = typer.Option(
        False,
        "--delegate-eval",
        help="Explorer sub-agent only: locate each task's defect from the bug report.",
    ),
    retrieval_eval: str | None = typer.Option(
        None,
        "--retrieval-eval",
        help="Retrieval only: recall@k/MRR for modes, e.g. bm25,vector,hybrid "
        "(vector modes use [context] embeddings or --embeddings).",
    ),
    embeddings: str | None = typer.Option(None, "--embeddings", help="provider:model"),
    compaction_eval: str | None = typer.Option(
        None,
        "--compaction-eval",
        help="Fact retention after context compaction, e.g. full,model,deterministic.",
    ),
) -> None:
    """Run the fixtures A–E, the suite, a comparison, an ablation, a sweep or a counterfactual."""
    from trendlab.benchmarks.runner import (
        ABLATIONS,
        CANARY,
        append_bench_log,
        compare_rows,
        run_benchmarks,
        run_suite,
        summarize,
        verify_suite_tasks,
    )

    if compaction_eval:
        from trendlab.benchmarks.compaction_eval import run as run_compaction
        from trendlab.config.loader import load_config
        from trendlab.telemetry.recorded import RecordedCaller

        cfg = load_config()

        async def _compaction():
            summ = RecordedCaller(cfg, "summarizer", "(benchmarks)", model=model)
            ans = RecordedCaller(cfg, "screener", "(benchmarks)", model=model)
            try:
                return [
                    await run_compaction(summ, ans, method=m.strip())
                    for m in compaction_eval.split(",")
                    if m.strip()
                ], round(summ.cost + ans.cost, 4)
            finally:
                await summ.close()
                await ans.close()

        results, cost = asyncio.run(_compaction())
        if output == "json":
            typer.echo(json.dumps({"results": results, "cost": cost}, indent=2))
        else:
            for r in results:
                console.print(
                    f"  {r['method']}: retention {r['retention']:.0%} of {r['facts']} facts · "
                    f"context {r['compression']:.0%} of full · lost {r['lost'] or 'none'}"
                )
            console.print(f"  cost ${cost:.4f}")
        return
    if retrieval_eval:
        from trendlab.benchmarks.runner import retrieval_eval as run_retrieval_eval
        from trendlab.benchmarks.runner import select_tasks
        from trendlab.config.loader import load_config
        from trendlab.context.vectors import make_embedder

        modes = [m.strip() for m in retrieval_eval.split(",") if m.strip()]
        cfg = load_config()
        ref = embeddings or cfg.context.embeddings
        embedder = make_embedder(cfg, ref) if ref and any(m != "bm25" for m in modes) else None
        chosen = [
            t
            for t in select_tasks(tasks, lang, tier, holdout=holdout)
            if t.lang != "go" and not t.answer_keywords and t.defect.old
        ]
        res = run_retrieval_eval(chosen, modes, embedder)
        if save_rows is not None:
            save_rows.write_text(json.dumps(res["rows"], indent=1))
        res.pop("rows")
        if output == "json":
            typer.echo(json.dumps(res, indent=2))
        else:
            for m, v in res["modes"].items():
                console.print(f"  {m}: all {v['all']} · symptom-only {v['symptom_only']}")
        return
    if delegate_eval:
        from trendlab.benchmarks.runner import (
            delegate_eval_task,
            select_tasks,
            summarize_delegate_eval,
        )

        chosen = [t for t in select_tasks(tasks, lang, tier, holdout=holdout) if t.defect.old]

        async def _delegate_all():
            out = []
            for t in chosen:
                row = await delegate_eval_task(t, model)
                out.append(row)
                if output != "json":
                    console.print(
                        f"  {t.id}: located={row['located']} line={row['line_hit']} "
                        f"handoff={row['handoff_complete']} ${row['cost']:.4f}"
                    )
            return out

        rows = asyncio.run(_delegate_all())
        summary = summarize_delegate_eval(rows)
        if save_rows is not None:
            save_rows.write_text(json.dumps(rows, indent=1, default=str))
        if output == "json":
            typer.echo(json.dumps({"summary": summary, "rows": rows}, indent=2, default=str))
        else:
            console.print(f"[neon]delegate eval[/neon] {model}: " + json.dumps(summary))
        return
    if review_eval:
        from trendlab.benchmarks.runner import (
            review_eval_task,
            select_tasks,
            summarize_review_eval,
        )
        from trendlab.config.loader import load_config
        from trendlab.telemetry.recorded import RecordedCaller

        # a contract defect's reference state removes the half-built feature instead of
        # completing it, so its "good" diff is not a fix; those tasks cannot score false alarms
        chosen = [
            t
            for t in select_tasks(tasks, lang, tier, holdout=holdout)
            if t.defect.old and t.defect.kind != "multi_file_contract"
        ]

        async def _review_all():
            call = RecordedCaller(load_config(), "reviewer", "(benchmarks)", model=model)
            out = []
            try:
                for t in chosen:
                    row = await review_eval_task(t, call, mode=review_mode, confirm=not no_confirm)
                    out.append(row)
                    if output != "json":
                        console.print(
                            f"  {t.id}: caught={row['caught']} localised={row['localised']} "
                            f"false_alarm={row['false_alarm']} lenses={row['lenses_catching']}"
                        )
            finally:
                cost = call.cost
                await call.close()
            return out, cost

        rows, cost = asyncio.run(_review_all())
        summary = {**summarize_review_eval(rows), "model": model, "cost": round(cost, 4)}
        if save_rows is not None:
            save_rows.write_text(json.dumps(rows, indent=1, default=str))
        if output == "json":
            typer.echo(json.dumps({"summary": summary, "rows": rows}, indent=2, default=str))
        else:
            console.print(f"[neon]review eval[/neon] {model}: " + json.dumps(summary))
        return
    if planning_eval:
        from trendlab.benchmarks.runner import (
            planning_eval_task,
            select_tasks,
            summarize_planning,
        )

        chosen = select_tasks(tasks, lang, tier, holdout=holdout)

        async def _plan_all():
            out = []
            for t in chosen:
                row = await planning_eval_task(t, model)
                out.append(row)
                if output != "json":
                    console.print(
                        f"  {t.id}: steps={row.get('steps')} recall={row.get('recall')} "
                        f"precision={row.get('precision')} deps={row.get('dependencies')} "
                        f"lint={len(row.get('lint_issues') or [])}→"
                        f"{len(row.get('lint_remaining') or [])} ${row['cost']:.4f}"
                    )
            return out

        rows = asyncio.run(_plan_all())
        summary = summarize_planning(rows)
        if save_rows is not None:
            save_rows.write_text(json.dumps(rows, indent=1, default=str))
        if output == "json":
            typer.echo(json.dumps({"summary": summary, "rows": rows}, indent=2, default=str))
        else:
            console.print(f"[neon]planning eval[/neon] {model}: " + json.dumps(summary))
        return
    if verify_suite:
        problems = verify_suite_tasks()
        if problems:
            for p in problems:
                console.print(f"[danger]✗[/danger] {p}")
            raise typer.Exit(code=1)
        console.print(
            "[ok]suite verified[/ok]: every task materialises, fails its hidden test before "
            "a fix, and has a clean baseline"
        )
        return
    if canary:
        suite, tasks, tier = True, CANARY, "all"
    if counterfactual is not None:
        rows_in = _load_rows(counterfactual)
        failed = sorted(
            {r["task"] for r in rows_in if not r.get("passes") and not r.get("skipped")}
        )
        if not failed:
            console.print("[ok]no failed tasks in that file[/ok]")
            return
        console.print(
            f"[neon]counterfactual[/neon]: rerunning {len(failed)} failed task(s) "
            f"under {model}@{profile}"
        )
        suite, tasks, tier = True, ",".join(failed), "all"
    if ablation:
        compare = [f"{model}@harness"] + [f"{model}@{a}" for a in ABLATIONS]
    if sweep:
        key, _, values = sweep.partition("=")
        compare = [f"{model}@harness+{key}={v}" for v in values.split(",") if v != ""]
    if suite or compare:
        configs = compare if compare else [f"{model}@{profile}"]
        if compare and len(configs) < 2:
            raise typer.BadParameter("--compare needs at least two model@profile values")
        summaries = []
        all_rows: list[list[dict]] = []
        for spec in configs:
            m, _, prof = spec.partition("@")
            prof = prof or "harness"
            console.print(f"[neon]suite[/neon] {m} · profile {prof} · {tasks or 'all'} tasks")

            def show(r):
                if r.get("skipped"):
                    console.print(f"  [dim]{r['task']}: skipped ({r['skipped']})[/dim]")
                    return
                marks = " ".join(
                    f"{k}={'✓' if r.get(k) else '✗'}"
                    for k in (
                        "located",
                        "root_cause",
                        "passes",
                        "no_collateral",
                        "regression_added",
                    )
                )
                console.print(f"  {r['task']}: {marks} ${r['cost']:.3f} {r['wall_s']:.0f}s")

            results = asyncio.run(
                run_suite(
                    m,
                    profile=prof,
                    selector=tasks,
                    lang=lang,
                    sandbox=sandbox,
                    on_result=show,
                    tier=tier,
                    runs=runs,
                    holdout=holdout,
                    concurrency=concurrency,
                    perturbation=perturb,
                    chaos=chaos,
                )
            )
            summary = {"config": spec, **summarize(results)}
            summaries.append(summary)
            all_rows.append(results)
            if log:
                append_bench_log(Path("docs/BENCH_LOG.md"), f"suite {spec}", results)
        cmp = compare_rows(all_rows[0], all_rows[1]) if len(summaries) == 2 else None
        if len(summaries) > 2:  # ablation / sweep: every variant against the first
            t2 = Table(title="Variants vs " + summaries[0]["config"])
            for col in (
                "variant",
                "passes",
                "Δ passes",
                "p",
                "cost",
                "cost ratio",
                "Δ lead tokens",
            ):
                t2.add_column(col)
            for sm, rows in zip(summaries[1:], all_rows[1:], strict=True):
                c = compare_rows(all_rows[0], rows)
                t2.add_row(
                    sm["config"].split("@")[-1],
                    str(sm["passes"]),
                    f"{c['deltas']['passes']:+}",
                    str(c["paired_passes"]["p_value"]),
                    f"${sm['cost']:.3f}",
                    f"{c['cost_ratio']}×",
                    f"{c['deltas']['tokens_lead']:+}",
                )
            console.print(t2)
        if save_rows is not None:
            tagged = [
                [{**r, "config": s["config"]} for r in rows]
                for s, rows in zip(summaries, all_rows, strict=True)
            ]
            save_rows.write_text(json.dumps({"rows": tagged}, indent=1, default=str))
            console.print(f"[ok]rows[/ok] {save_rows}")
        if scorecard_path is not None:
            from trendlab.benchmarks.runner import scorecard

            scorecard_path.write_text(scorecard(summaries, cmp) + "\n", encoding="utf-8")
            console.print(f"[ok]scorecard[/ok] {scorecard_path}")
        if output == "json":
            typer.echo(json.dumps({"summaries": summaries, "compare": cmp}, indent=2, default=str))
            if gate and cmp and not cmp["gate"]["ok"]:
                raise typer.Exit(code=1)
            return
        cols = (
            "config",
            "tasks",
            "passes",
            "passes_ci95",
            "pass_at_k",
            "flaky_tasks",
            "no_collateral",
            "regression_added",
            "tool_success_rate",
            "cost",
            "cost_per_task_ci95",
        )
        t = Table(title="Suite summary")
        for col in cols:
            t.add_column(col)
        for sm in summaries:
            t.add_row(*(str(sm.get(c, "")) for c in cols))
        console.print(t)
        if cmp is not None:
            deltas = cmp["deltas"]
            pp = cmp["paired_passes"]
            console.print(
                "[neon]Δ (second − first):[/neon] "
                + ", ".join(f"{k} {v:+}" for k, v in deltas.items())
            )
            console.print(
                f"[neon]paired passes:[/neon] second wins {pp['b_wins']}, loses {pp['b_losses']}, "
                f"ties {pp['ties']} of {pp['tasks']} tasks · exact sign test p={pp['p_value']} · "
                f"cost ratio {cmp['cost_ratio']}×"
            )
            if pp["p_value"] > 0.05:
                console.print("[dim]not a significant difference at this sample size[/dim]")
            if log:
                append_bench_log(
                    Path("docs/BENCH_LOG.md"),
                    "compare",
                    [{"config": s["config"], **{k: s[k] for k in deltas}} for s in summaries],
                    note="Δ: "
                    + json.dumps(deltas)
                    + " · paired: "
                    + json.dumps(pp)
                    + f" · cost ratio {cmp['cost_ratio']}",
                )
            if gate:
                g = cmp["gate"]
                if g["ok"]:
                    console.print("[ok]gate passed[/ok]")
                else:
                    console.print("[danger]gate failed:[/danger] " + "; ".join(g["reasons"]))
                    raise typer.Exit(code=1)
        return
    results = asyncio.run(run_benchmarks(model, fixture))
    if output == "json":
        typer.echo(json.dumps(results, indent=2))
        return
    t = Table(title=f"Benchmark — {model}")
    for col in (
        "fixture",
        "success",
        "model calls",
        "tool calls",
        "cost",
        "elapsed",
        "files changed",
    ):
        t.add_column(col)
    for r in results:
        t.add_row(
            r["fixture"],
            "✓" if r["success"] else "✗",
            str(r["model_calls"]),
            str(r["tool_calls"]),
            f"${r['cost_usd']:.3f}",
            f"{r['elapsed_s']:.0f}s",
            str(r["files_changed"]),
        )
    console.print(t)


# -- trendlab secret ... --------------------------------------------------------------------
@secret_app.command("set")
def secret_set(name: str = typer.Argument(..., help="e.g. ANTHROPIC_API_KEY")) -> None:
    """Store a secret. The value is prompted without echo and never printed back."""
    from trendlab.security.secrets import store_secret

    value = typer.prompt(f"Paste the value for {name} (input hidden)", hide_input=True)
    try:
        path = store_secret(name, value)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    console.print(
        f"[green]Stored {name}[/green] in {path.parent} (mode 0600). It is never displayed."
    )


@secret_app.command("list")
def secret_list() -> None:
    from trendlab.security.secrets import list_secrets

    names = list_secrets()
    console.print("\n".join(names) if names else "[dim]no stored secrets[/dim]")


@secret_app.command("rm")
def secret_rm(name: str) -> None:
    from trendlab.security.secrets import delete_secret

    console.print(f"removed {name}" if delete_secret(name) else f"[dim]{name} was not stored[/dim]")


# -- trendlab doctor / init -----------------------------------------------------------------------
def _update_hint() -> None:
    """Once a day: a dim one-liner when a newer release exists. Never blocks startup for long."""
    if os.environ.get("TRENDLAB_NO_UPDATE_CHECK"):
        return
    try:
        from trendlab.update import daily_hint

        hint = daily_hint()
    except Exception:  # noqa: BLE001 — never let the check break the CLI
        return
    if hint:
        console.print(f"[dim]{hint}[/dim]")


@app.command("update")
def update_cmd(
    check_only: bool = typer.Option(
        True, "--check/--run", help="Check only (default) or run the upgrade command."
    ),
) -> None:
    """Check PyPI / GitHub Releases for a newer TrendLab and show how to upgrade."""
    from trendlab.update import check_for_update, install_command, write_cache

    info = check_for_update()
    write_cache(info)
    if info.error and not info.latest:
        console.print(f"[warning]could not check for updates: {info.error}[/warning]")
        raise typer.Exit(1)
    if not info.available:
        console.print(
            f"[ok]TrendLab {info.current} is up to date[/ok]"
            + (f" [dim](latest on {info.source}: {info.latest})[/dim]" if info.latest else "")
        )
        return
    cmd = install_command(info.source)
    console.print(f"[accent]TrendLab {info.latest} is available[/accent] (you have {info.current})")
    console.print(f"[dim]{info.url}[/dim]")
    if check_only:
        console.print(f"Upgrade with:  [bold]{cmd}[/bold]   (or: trendlab update --run)")
        return
    import shlex
    import subprocess

    console.print(f"[dim]$ {cmd}[/dim]")
    code = subprocess.call(shlex.split(cmd.split("  (")[0]))
    raise typer.Exit(code)


@app.command("keys")
def keys_cmd() -> None:
    """Show what your terminal sends for each key (debug Ctrl+V / Alt+V paste problems)."""
    from trendlab.ui.keyprobe import main as probe

    probe()


@app.command("stub")
def stub_cmd(
    spec: Path | None = typer.Option(None, "--spec", help="OpenAPI JSON/YAML or a recording JSON."),
    port: int = typer.Option(8089, "--port"),
    record: str | None = typer.Option(
        None, "--record", help="Allow-listed host to proxy and record once."
    ),
    record_to: Path = typer.Option(Path(".trendlab/stubs/recorded.json"), "--record-to"),
) -> None:
    """Serve a local HTTP stub for tests that call external services (spec §8.5)."""
    import time as _time

    from trendlab.tools.stubs import StubServer

    server = StubServer(
        spec=spec, port=port, record_host=record, record_to=record_to if record else None
    )
    server.start()
    console.print(
        f"[ok]stub[/ok] listening on {server.url} · {len(server.routes)} routes"
        + (f" · recording {record} → {record_to}" if record else "")
        + " · Ctrl+C to stop"
    )
    try:
        while True:
            _time.sleep(1)
    except KeyboardInterrupt:
        server.stop()


@app.command("replay")
def replay_cmd(
    session_id: str = typer.Argument(
        "", help="Stored session id (see `trendlab sessions`); omit with --recent."
    ),
    model: str | None = typer.Option(
        None, "--model", "-m", help="Model for the replay (default: the session's)."
    ),
    recent: int = typer.Option(
        0, "--recent", help="With --deterministic: replay the N latest recorded sessions."
    ),
    max_prompts: int | None = typer.Option(None, "--max-prompts"),
    deterministic: bool = typer.Option(
        False,
        "--deterministic",
        help="Serve every model response from the session's cassette ($0): a regression test of "
        "the harness against the real session.",
    ),
    output: str = typer.Option("text", help="text | json"),
) -> None:
    """Shadow replay: re-run a stored session's prompts against the current build (spec §8.6);
    --deterministic replays the recorded model responses instead of calling a model (U20)."""
    from trendlab.benchmarks.replay import replay_session
    from trendlab.config.loader import load_config, trendlab_home
    from trendlab.sessions.store import SessionStore

    config = load_config()
    store = SessionStore(trendlab_home() / "sessions.db")
    if deterministic and recent:
        from trendlab.benchmarks.cassette import cassette_dir, deterministic_replay

        files = sorted(cassette_dir().glob("*.jsonl"), key=lambda f: f.stat().st_mtime)[-recent:]
        reports = []
        try:
            for f in files:
                if store.get_session(f.stem) is None:
                    continue
                reports.append(
                    asyncio.run(deterministic_replay(store, f.stem, config=load_config()))
                )
        finally:
            store.close()
        ok = [r for r in reports if r.get("identical")]
        bad = [r for r in reports if not r.get("identical")]
        summary = {
            "replayed": len(reports),
            "identical": len(ok),
            "diverged": [
                {"session": r["session"], "why": (r.get("divergences") or [r.get("error")])[:2]}
                for r in bad
            ],
            "model_calls_served": sum(r.get("model_calls_served", 0) for r in reports),
        }
        if output == "json":
            typer.echo(json.dumps(summary, indent=2))
        else:
            console.print(
                f"[neon]replay regression[/neon]: {summary['identical']}/{summary['replayed']} "
                f"sessions identical · {summary['model_calls_served']} model calls from cassettes "
                "· $0"
            )
            for d in summary["diverged"]:
                console.print(f"  [danger]✗[/danger] {d['session']}: {d['why']}")
        raise typer.Exit(code=0 if not bad else 1)
    if not session_id:
        raise typer.BadParameter("give a session id, or --deterministic --recent N")
    if deterministic:
        from trendlab.benchmarks.cassette import deterministic_replay

        try:
            report = asyncio.run(deterministic_replay(store, session_id, config=config))
        finally:
            store.close()
        if output == "json" or "error" in report:
            typer.echo(json.dumps(report, indent=2))
        else:
            verdict = "[ok]identical[/ok]" if report["identical"] else "[danger]diverged[/danger]"
            console.print(
                f"replay {session_id}: {verdict} · {report['model_calls_served']}/"
                f"{report['model_calls_recorded']} recorded model calls served · tools "
                f"{report['tool_calls_before']} → {report['tool_calls_after']} · outcomes "
                f"{report['outcomes_before']} → {report['outcomes_after']} · $0"
            )
            for d in report["divergences"]:
                console.print(f"  · {d}")
        raise typer.Exit(code=0 if report.get("identical") else 1)
    try:
        report = asyncio.run(
            replay_session(store, session_id, config=config, model=model, max_prompts=max_prompts)
        )
    finally:
        store.close()
    out_dir = trendlab_home() / "replays"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{session_id}.json").write_text(json.dumps(report, indent=1))
    if output == "json" or "error" in report:
        typer.echo(json.dumps(report, indent=2))
        return
    t = Table(title=f"Replay {session_id}")
    t.add_column("metric")
    t.add_column("before")
    t.add_column("after")
    for k in ("model", "tool_calls", "assistant_turns", "cost"):
        t.add_row(k, str(report.get(f"{k}_before")), str(report.get(f"{k}_after")))
    t.add_row("outcomes", "", ", ".join(report["outcomes"]))
    t.add_row("shared tool prefix", "", str(report["tool_sequence_shared_prefix"]))
    t.add_row("replayed / live calls", "", f"{report['replayed_results']} / {report['live_calls']}")
    console.print(t)
    console.print(f"[dim]saved {out_dir / (session_id + '.json')}[/dim]")


engine_app = typer.Typer(
    help="The local engine: inbox, watch, sleeptime pass, meta-loop (spec §7)."
)
app.add_typer(engine_app, name="engine")


def _engine_projects(config, project: Path | None) -> list[Path]:
    roots = [Path(p).expanduser() for p in config.engine.projects]
    if project is not None:
        roots.append(project)
    return roots or [Path.cwd()]


@engine_app.command("start")
def engine_start(
    project: Path | None = typer.Option(
        None, "--project", "-C", help="Extra project root to watch."
    ),
    foreground: bool = typer.Option(False, "--foreground", help="Run in this terminal."),
) -> None:
    """Start the engine daemon (scheduler + inbox + socket)."""
    import os
    import subprocess
    import sys

    from trendlab.config.loader import load_config
    from trendlab.engine.daemon import Engine, read_pid, socket_path

    if read_pid():
        console.print(f"[warning]engine already running[/warning] (pid {read_pid()})")
        return
    config = load_config()
    projects = _engine_projects(config, project)
    if not foreground:
        log = trendlab_home() / "engine" / "engine.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        args = [sys.executable, "-m", "trendlab.cli", "engine", "start", "--foreground"]
        if project is not None:
            args += ["--project", str(project)]
        with log.open("ab") as fh:
            proc = subprocess.Popen(
                args,
                stdout=fh,
                stderr=fh,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env={**os.environ, "TRENDLAB_TELEGRAM": "off"},
            )
        console.print(
            f"[ok]engine started[/ok] pid {proc.pid} · socket {socket_path()} · log {log}"
        )
        return
    asyncio.run(Engine(projects=projects, config=config).run())


@engine_app.command("stop")
def engine_stop() -> None:
    """Stop the running engine."""
    from trendlab.engine.daemon import client, read_pid

    pid = read_pid()
    if not pid:
        console.print("[dim]engine is not running[/dim]")
        return
    asyncio.run(client("stop"))
    console.print(f"[ok]stop requested[/ok] (pid {pid})")


@engine_app.command("status")
def engine_status() -> None:
    """Show the engine's schedule state and inbox counts."""
    from trendlab.engine.daemon import client, read_pid

    resp = asyncio.run(client("status"))
    if resp is None:
        console.print(f"[dim]engine is not running[/dim] (pid file: {read_pid()})")
        return
    typer.echo(json.dumps(resp, indent=2))


@engine_app.command("run")
def engine_run(job: str = typer.Argument(..., help="watch | sleep | meta | digest")) -> None:
    """Run one engine job now (through the daemon when it runs, else in-process)."""
    from trendlab.config.loader import load_config
    from trendlab.engine.daemon import Engine, client

    resp = asyncio.run(client("run", job=job))
    if resp is None:
        config = load_config()
        eng = Engine(projects=_engine_projects(config, None), config=config)
        resp = asyncio.run(eng.run_job(job))
        eng.inbox.close()
    typer.echo(json.dumps(resp, indent=2, default=str))


@app.command("watch")
def watch_cmd(project: Path = typer.Option(Path.cwd(), "--project", "-C")) -> None:
    """Run the project's tests and file failures as inbox cards (spec §7.2)."""
    from trendlab.config.loader import load_config
    from trendlab.engine.daemon import inbox_path
    from trendlab.engine.inbox import Inbox
    from trendlab.engine.watch import watch_project

    inbox = Inbox(inbox_path())
    try:
        issues = asyncio.run(watch_project(inbox, project.resolve(), load_config(project)))
    finally:
        inbox.close()
    console.print(
        f"[ok]watch[/ok] {len(issues)} issue(s) filed" if issues else "[ok]watch[/ok] tests green"
    )


@app.command("sleep")
def sleep_cmd(
    project: Path = typer.Option(Path.cwd(), "--project", "-C"),
    no_branch: bool = typer.Option(
        False, "--no-branch", help="Write files only; no review branch."
    ),
) -> None:
    """Sleeptime pass: consolidate memory, propose instructions/skills, open a review branch."""
    from trendlab.config.loader import load_config
    from trendlab.engine.jobs import _caller
    from trendlab.engine.sleep import sleeptime
    from trendlab.sessions.store import SessionStore

    config = load_config(project)
    store = SessionStore(trendlab_home() / "sessions.db")
    try:
        res = asyncio.run(
            sleeptime(
                _caller(config, "summarizer"),
                store=store,
                project_root=project.resolve(),
                model_ref=config.defaults.model,
                branch=not no_branch,
            )
        )
    finally:
        store.close()
    typer.echo(json.dumps(res, indent=2))


@app.command("meta")
def meta_cmd(
    days: int = typer.Option(7, "--days"),
    draft: bool = typer.Option(
        False, "--draft", help="Draft a fix for the top harness card in a worktree of trendlab-cli."
    ),
) -> None:
    """Meta-loop: screen the harness's own sessions and file cards against the harness."""
    from trendlab.config.loader import load_config
    from trendlab.engine.daemon import inbox_path
    from trendlab.engine.inbox import Inbox
    from trendlab.engine.jobs import meta_draft, meta_scan

    config = load_config()
    inbox = Inbox(inbox_path())
    try:
        res = asyncio.run(meta_scan(inbox, config, days=days))
        if draft:
            res["draft"] = asyncio.run(meta_draft(inbox, config))
    finally:
        inbox.close()
    typer.echo(json.dumps(res, indent=2, default=str))


@app.command("failures")
def failures_cmd(
    days: int = typer.Option(7, "--days"),
    output: str = typer.Option("text", help="text | json"),
) -> None:
    """Failed runs by taxonomy code, ranked as a failure-mode table (severity x occurrence x
    detection). Codes and remedies come from trendlab/agent/taxonomy.py (U8)."""
    from trendlab.agent.taxonomy import CODES, fmea
    from trendlab.sessions.store import SessionStore

    store = SessionStore(trendlab_home() / "sessions.db")
    try:
        st = store.stats(days)
    finally:
        store.close()
    runs = sum(st["runs"].values())
    table = fmea(st.get("failure_codes", {}), runs)
    if output == "json":
        typer.echo(json.dumps({"runs": runs, "failures": table}, indent=2))
        return
    if not table:
        console.print(f"[ok]no failed runs in the last {days} days[/ok] ({runs} runs)")
        return
    console.print(f"[neon]Failure modes, last {days} days[/neon] · {runs} runs")
    console.print("  RPN  S O D  count  code — what it means · first remedy")
    for r in table:
        c = CODES.get(r["code"], CODES["UNKNOWN"])
        console.print(
            f"  {r['rpn']:>3}  {r['severity']} {r['occurrence']} {r['detection']}  {r['count']:>5}"
            f"  {r['code']} — {c.summary} · {r['remedy']}"
        )


@app.command("rca")
def rca_cmd(
    session_id: str = typer.Argument(..., help="Session id (see `trendlab sessions`)."),
    output: str = typer.Option("text", help="text | json"),
) -> None:
    """Root-cause analysis of a session's runs: the failure code, the first warning sign, the
    chain of signals before the stop, likely causes, remedies and files to suspect (U8)."""
    from rich.markup import escape

    from trendlab.agent.taxonomy import rca
    from trendlab.sessions.store import SessionStore

    store = SessionStore(trendlab_home() / "sessions.db")
    try:
        runs = rca(store.events(session_id))
    finally:
        store.close()
    if output == "json":
        typer.echo(json.dumps(runs, indent=2, default=str))
        return
    if not runs:
        console.print("[dim]no finished runs in this session[/dim]")
        return
    for i, r in enumerate(runs, 1):
        head = f"run {i} · {r['status']}" + (
            f" · {r['code']} ({r['summary']})" if r["code"] else ""
        )
        console.print(f"[neon]{escape(head)}[/neon]")
        if r["stop_reason"]:
            console.print(f"  stopped: {escape(str(r['stop_reason'])[:200])}")
        if r["first_sign"]:
            console.print(f"  first sign: {escape(r['first_sign'])}")
        for s in r["signals"]:
            console.print(f"    · {escape(s)}")
        for label in ("likely_causes", "remedies", "suspects"):
            if r.get(label):
                console.print(f"  {label.replace('_', ' ')}: " + escape("; ".join(r[label])))


@app.command("turns")
def turns_cmd(
    session_id: str = typer.Argument("latest", help="Session id, or 'latest' for this project."),
    project: Path = typer.Option(Path.cwd(), "--project", "-C"),
    patch: bool = typer.Option(False, "--patch", help="Show full diffs, not just file stats."),
) -> None:
    """Turn-level review: one commit per completed turn on trendlab/turns/<session> (needs
    [governance] commit_per_turn = true)."""
    from trendlab.sessions.store import SessionStore
    from trendlab.sessions.turns import branch_for, list_turns

    root = project.resolve()
    if session_id == "latest":
        store = SessionStore(trendlab_home() / "sessions.db")
        try:
            latest = store.latest_session(str(root))
        finally:
            store.close()
        if latest is None:
            console.print("[dim]no session for this project[/dim]")
            raise typer.Exit(code=1)
        session_id = latest["id"]
    log = list_turns(root, session_id, patch=patch)
    if not log:
        console.print(
            f"[dim]no turn commits on {branch_for(session_id)} (turn commits are off unless "
            "[governance] commit_per_turn = true)[/dim]"
        )
        return
    typer.echo(log)


@app.command("cost")
def cost_cmd(
    days: int = typer.Option(30, "--days"),
    project: str | None = typer.Option(None, "--project", help="Only this project path."),
    output: str = typer.Option("text", help="text | json"),
) -> None:
    """Cost ledger (U10): spend per project with ROI, overhead, waste and cache share; this
    period against the previous one; month-to-date budgets."""
    from trendlab.config.loader import load_config
    from trendlab.sessions.store import SessionStore
    from trendlab.telemetry.ledger import bucket, budget_status, ledger

    store = SessionStore(trendlab_home() / "sessions.db")
    try:
        led = ledger(store, days)
        budgets = budget_status(store, load_config().economics)
    finally:
        store.close()
    if project:
        key = bucket(str(Path(project).expanduser().resolve()))
        led["projects"] = [r for r in led["projects"] if r["project"] == key]
    if output == "json":
        typer.echo(json.dumps({**led, "budgets": budgets}, indent=2))
        return
    change = (
        f" ({led['change']:+.0%} vs the {days} days before)" if led["change"] is not None else ""
    )
    console.print(
        f"[neon]Spend, last {days} days[/neon]: ${led['total']:.3f}{change} · "
        f"project work {led['work_share'] or 0:.0%} · wasted on runs that never completed "
        f"${led['wasted']:.3f}"
    )
    for r in led["projects"][:15]:
        roi = (
            f"${r['cost_per_validated_change']:.3f}/validated change"
            if r["cost_per_validated_change"]
            else "no validated changes"
        )
        console.print(
            f"  ${r['cost']:.3f}  {r['project']} · {r['runs']} runs ({r['completed']} done) · "
            f"{roi} · overhead {r['overhead_share'] or 0:.0%} · cache {r['cache_share'] or 0:.0%}"
        )
    for b in budgets:
        console.print(
            f"  budget {b['budget']}: ${b['spent']:.2f} of ${b['limit']:.2f} ({b['share']:.0%})"
        )


def _review_source(root: Path, base: str | None, range_: str | None, pr: int | None):
    """(diff, intent, label) for a pull request, a commit range, or the branch + working tree
    against its merge base with ``base`` (default: the remote's default branch, else main)."""
    import subprocess as sp

    def git(*args):
        return sp.run(["git", "-C", str(root), *args], capture_output=True, text=True).stdout

    if pr is not None:
        diff = sp.run(
            ["gh", "pr", "diff", str(pr)], cwd=root, capture_output=True, text=True
        ).stdout
        meta = sp.run(
            ["gh", "pr", "view", str(pr), "--json", "title,body"],
            cwd=root,
            capture_output=True,
            text=True,
        ).stdout
        try:
            m = json.loads(meta)
            intent = f"{m.get('title', '')}\n\n{m.get('body', '')}"
        except ValueError:
            intent = ""
        return diff, intent, f"PR #{pr}"
    if range_:
        return git("diff", range_), git("log", "--format=%s%n%b", range_), range_
    if base is None:
        head = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip()
        base = head.split("/", 1)[1] if "/" in head else "main"
    mb = git("merge-base", base, "HEAD").strip() or base
    diff = git("diff", mb)  # committed on the branch + uncommitted changes
    untracked = git("ls-files", "--others", "--exclude-standard").split()
    for f in untracked[:50]:
        diff += git("diff", "--no-index", "/dev/null", f)
    return diff, git("log", "--format=%s%n%b", f"{mb}..HEAD"), f"branch vs {base}"


@app.command("review")
def review_cmd(
    project: Path = typer.Option(Path.cwd(), "--project", "-C"),
    base: str | None = typer.Option(None, "--base", help="Review the branch against this ref."),
    range_: str | None = typer.Option(None, "--range", help="Review a commit range, e.g. A..B."),
    pr: int | None = typer.Option(None, "--pr", help="Review a GitHub pull request (needs gh)."),
    mode: str = typer.Option("auto", "--mode", help="auto | quick (one call) | deep (per lens)."),
    model: str | None = typer.Option(None, "--model", "-m", help="Reviewer model."),
    recheck: bool = typer.Option(False, "--recheck", help="Re-check the last review's findings."),
    fix: bool = typer.Option(False, "--fix", help="Hand the open findings to the agent."),
    pre_pr: bool = typer.Option(
        False, "--pre-pr", help="Run validation + review; exit 1 on failures or blocking findings."
    ),
    output: str = typer.Option("text", help="text | json"),
) -> None:
    """Code review of a branch, range or PR (U13): deterministic checks + focused lenses
    (correctness, edge cases, structure, performance, tests, security), a findings ledger in
    .trendlab/reviews.json, re-check closure, and a pre-PR gate."""
    import subprocess as sp

    from rich.markup import escape

    from trendlab.agent.review import (
        apply_recheck,
        fix_prompt,
        is_closed,
        load_ledger,
        review_diff,
        save_review,
        static_findings,
    )
    from trendlab.agent.review import (
        recheck as do_recheck,
    )
    from trendlab.config.loader import load_config
    from trendlab.telemetry.recorded import RecordedCaller

    root = project.resolve()
    config = load_config(root)
    diff, intent, label = _review_source(root, base, range_, pr)
    if fix:
        led = load_ledger(root)
        if not led["reviews"]:
            console.print("[dim]no review yet; run `trendlab review` first[/dim]")
            raise typer.Exit(code=1)
        prompt = fix_prompt(led["reviews"][-1])
        rc = sp.run(
            [sys.executable, "-m", "trendlab.cli", "-C", str(root), "-p", prompt]
        ).returncode
        raise typer.Exit(code=rc)
    if not diff.strip():
        console.print(f"[dim]nothing to review ({label})[/dim]")
        return
    call = RecordedCaller(
        config,
        "reviewer",
        str(root),
        model=model or config.routing.get("reviewer", config.routing.get("verifier")),
    )
    try:
        if recheck:
            led = load_ledger(root)
            if not led["reviews"]:
                console.print("[dim]no review to re-check[/dim]")
                raise typer.Exit(code=1)
            review = led["reviews"][-1]
            outcome = asyncio.run(do_recheck(call, review["findings"], diff))
            review = apply_recheck(review, outcome, [f["id"] for f in static_findings(diff)])
        else:
            res = asyncio.run(review_diff(call, diff, intent=intent, mode=mode))
            review = {"source": label, "model": call.ref, **res}
        validation_ok = None
        if pre_pr:
            from trendlab.context.validation import detect_validation_commands

            cmds = detect_validation_commands(root)
            for kind in ("test", "lint"):
                if cmds.get(kind):
                    ok = sp.run(cmds[kind], shell=True, cwd=root).returncode == 0
                    validation_ok = ok if validation_ok is None else validation_ok and ok
        review["cost"] = round(call.cost, 4)
        review["closed"] = is_closed(review, validation_ok)
        review = save_review(root, review)
    finally:
        asyncio.run(call.close())
    if output == "json":
        typer.echo(json.dumps(review, indent=2))
    else:
        open_ = [
            f for f in review["findings"] if f.get("status") == "open" and not f.get("dropped")
        ]
        dropped = sum(1 for f in review["findings"] if f.get("dropped"))
        fixed = sum(1 for f in review["findings"] if f.get("status") == "fixed")
        console.print(
            f"[neon]review {review['id']}[/neon] · {label} · {review.get('mode', 'recheck')} · "
            f"{len(open_)} open, {fixed} fixed, {dropped} dropped on confirmation · "
            f"${review['cost']:.4f}"
        )
        for f in open_:
            where = f["file"] + (f":{f['line']}" if f.get("line") else "")
            console.print(f"  [{f['severity']}] {f['lens']} {escape(where)} — {escape(f['issue'])}")
        console.print("  closed ✓" if review["closed"] else "  not closed: blocking findings open")
    if pre_pr and not review["closed"]:
        raise typer.Exit(code=1)


@app.command("health")
def health_cmd(
    project: Path = typer.Option(Path.cwd(), "--project", "-C"),
    backfill_n: int = typer.Option(0, "--backfill", help="Also measure N past commits."),
    step: int = typer.Option(5, "--step", help="Backfill every STEP-th commit."),
    save: bool = typer.Option(False, "--save", help="Append this snapshot to the history."),
    output: str = typer.Option("text", help="text | json"),
) -> None:
    """Code health (U14): size-adjusted function length and complexity, test ratio,
    duplication, debt markers and dependencies, with the trend against the 7-day baseline or a
    backfill of past commits."""
    from datetime import datetime

    from trendlab.engine.health import backfill, baseline, history, record, snapshot, trend

    root = project.resolve()
    snap = snapshot(root)
    past = backfill(root, backfill_n, step) if backfill_n else []
    base = past[0] if past else baseline(history(root))
    t = trend(base, snap) if base else None
    if save:
        record(root, snap, at=datetime.now().astimezone().isoformat(timespec="seconds"))
    if output == "json":
        typer.echo(
            json.dumps({"snapshot": snap, "baseline": base, "trend": t, "backfill": past}, indent=2)
        )
        return
    console.print(
        f"[neon]health[/neon] {root.name}: {snap['source_lines']:,} source lines · test ratio "
        f"{snap['test_ratio']} · long functions {snap['long_function_share'] or 0:.1%} · complex "
        f"{snap['complex_function_share'] or 0:.1%} · mean complexity {snap['mean_complexity']} "
        f"· duplication {snap['duplication']:.2%} · debt markers {snap['debt_markers']}"
    )
    for h in past:
        console.print(
            f"  {h['commit']} {h['date'][:10]} {h['source_lines']:>7,} lines · tests "
            f"{h['test_ratio']} · long {h['long_function_share'] or 0:.1%} · complex "
            f"{h['complex_function_share'] or 0:.1%}"
        )
    if t:
        verdict = (
            "[danger]degraded[/danger]: " + ", ".join(t["worse"])
            if t["degraded"]
            else ("[ok]no degradation[/ok]")
        )
        console.print(
            f"  vs baseline: {verdict}"
            + (f" · better: {', '.join(t['better'])}" if t["better"] else "")
        )


@app.command("critique")
def critique_cmd(
    doc: Path | None = typer.Argument(None, help="Design or plan document to critique."),
    models: str = typer.Option(
        "", "--models", help="Comma-separated reviewer models (default: the verifier role)."
    ),
    adversary: str | None = typer.Option(
        None, "--adversary", help="Model that argues the design will fail (default: first)."
    ),
    no_adversary: bool = typer.Option(False, "--no-adversary"),
    eval_: bool = typer.Option(
        False, "--eval", help="Score the panel on built-in designs with planted flaws."
    ),
    hard: bool = typer.Option(False, "--hard", help="With --eval: the subtler design set."),
    output: str = typer.Option("text", help="text | json"),
) -> None:
    """Multi-model design review (U15): critics per model plus an adversary; agreed points are
    consensus, single-reviewer points are dissent."""
    from rich.markup import escape

    from trendlab.agent.critique import critique
    from trendlab.config.loader import load_config
    from trendlab.telemetry.recorded import RecordedCaller

    config = load_config()
    names = [m.strip() for m in models.split(",") if m.strip()] or [
        config.routing.get("verifier", config.defaults.model)
    ]
    where = str(doc.resolve().parent) if doc else "(benchmarks)"
    callers = {n: RecordedCaller(config, "critic", where, model=n) for n in names}
    adv = None
    if not no_adversary:
        adv = RecordedCaller(config, "adversary", where, model=adversary or names[0])

    async def go():
        try:
            if eval_:
                from trendlab.benchmarks.runner import critique_eval

                return await critique_eval(callers, adversary=adv, hard=hard)
            if doc is None:
                raise typer.BadParameter("give a document, or --eval")
            return await critique(doc.read_text(), callers, adversary=adv)
        finally:
            for c in [*callers.values(), *([adv] if adv else [])]:
                await c.close()

    res = asyncio.run(go())
    cost = sum(c.cost for c in callers.values()) + (adv.cost if adv else 0.0)
    res["cost"] = round(cost, 4)
    if output == "json":
        res.pop("raw", None)
        typer.echo(json.dumps(res, indent=2, default=str))
        return
    if eval_:
        console.print(
            f"[neon]critique eval[/neon] · {res['flaws']} planted flaws in {res['designs']} "
            f"designs · recall {json.dumps(res['recall'])} · ${cost:.4f}"
        )
        return
    console.print(f"[neon]design review[/neon] · {', '.join(res['reviewers'])} · ${cost:.4f}")
    for p in res["points"]:
        tag = "consensus" if p["consensus"] else "dissent"
        console.print(
            f"  [{p['severity']}] {tag} ({', '.join(p['reviewers'])}): {escape(p['claim'])}"
        )


@app.command("spec")
def spec_cmd(
    spec: Path | None = typer.Argument(None, help="Specification file (markdown or text)."),
    project: Path = typer.Option(Path.cwd(), "--project", "-C"),
    model: str | None = typer.Option(None, "--model", "-m"),
    eval_: bool = typer.Option(
        False, "--eval", help="Score the checker on a built-in spec with known answers."
    ),
    output: str = typer.Option("text", help="text | json"),
) -> None:
    """Specification traceability (U19): each requirement → implemented / partial / missing,
    tested or not, with verified path:line evidence; drift against the previous check."""
    import tempfile

    from rich.markup import escape

    from trendlab.agent.spec import (
        SHOP_SPEC,
        SHOP_TRUTH,
        check,
        drift,
        extract,
        state_path,
    )
    from trendlab.config.loader import load_config
    from trendlab.telemetry.recorded import RecordedCaller

    config = load_config()
    ref = model or config.routing.get("verifier", config.defaults.model)

    async def go(root: Path, text: str, where: str):
        call = RecordedCaller(config, "spec", where, model=ref)
        try:
            reqs = await extract(call, text)
            res = await check(call, root, reqs)
            res["cost"] = round(call.cost, 4)
            return res
        finally:
            await call.close()

    if eval_:
        from trendlab.benchmarks import suite as suite_mod

        with tempfile.TemporaryDirectory(prefix="trendlab-suite-spec-") as tmp:
            root = Path(tmp) / "repo"
            suite_mod.materialize(suite_mod.get_task("py01-off_by_one"), root)
            (root / "shop/util.py").write_text(suite_mod.PY_BASE["shop/util.py"])  # no defect
            res = asyncio.run(go(root, SHOP_SPEC, "(benchmarks)"))
        got = {r["id"]: r["status"] for r in res["results"]}
        right = [i for i, st in SHOP_TRUTH.items() if got.get(i) == st]
        res["accuracy"] = round(len(right) / len(SHOP_TRUTH), 3)
        res["wrong"] = {
            i: {"expected": st, "got": got.get(i)}
            for i, st in SHOP_TRUTH.items()
            if got.get(i) != st
        }
        if output == "json":
            typer.echo(json.dumps(res, indent=2))
        else:
            console.print(
                f"[neon]spec eval[/neon] {ref}: accuracy {res['accuracy']:.0%} on "
                f"{len(SHOP_TRUTH)} requirements · evidence verified "
                f"{res['evidence_verified_share']:.0%} · ${res['cost']:.4f} · wrong: {res['wrong']}"
            )
        return
    if spec is None:
        raise typer.BadParameter("give a spec file, or --eval")
    root = project.resolve()
    prev = None
    try:
        prev = json.loads(state_path(root).read_text())
    except (OSError, ValueError):
        pass
    res = asyncio.run(go(root, spec.read_text(), str(root)))
    res["drift"] = drift(prev, res)
    state_path(root).parent.mkdir(parents=True, exist_ok=True)
    state_path(root).write_text(json.dumps(res, indent=1))
    if output == "json":
        typer.echo(json.dumps(res, indent=2))
        return
    console.print(
        f"[neon]spec[/neon] {spec.name}: {res['implemented']}/{res['requirements']} implemented, "
        f"{res['partial']} partial, {len(res['missing'])} missing · tested "
        f"{res['tested_share']:.0%} · evidence verified {res['evidence_verified_share']:.0%} · "
        f"${res['cost']:.4f}"
    )
    for r in res["results"]:
        mark = {"implemented": "✓", "partial": "~", "missing": "✗"}[r["status"]]
        console.print(
            f"  {mark} {r['id']} {escape(r['text'][:80])}"
            + (" [tested]" if r["tested"] else "")
            + (f" — {escape(r['note'][:80])}" if r["status"] != "implemented" else "")
        )
    for d in res["drift"]:
        console.print(f"  [danger]drift[/danger] {d['id']}: {d['was']} → {d['now']}")


@app.command("arch")
def arch_cmd(
    project: Path = typer.Option(Path.cwd(), "--project", "-C"),
    rule: list[str] = typer.Option(
        [], "--rule", help="Forbidden import edge 'pkg.low -> pkg.high' (adds to config)."
    ),
    output: str = typer.Option("text", help="text | json"),
) -> None:
    """Architecture check (U24): import graph, cycles, rule violations, big modules."""
    from trendlab.agent.arch import report
    from trendlab.config.loader import load_config

    root = project.resolve()
    rules = list(load_config(root).governance.forbid_imports) + list(rule)
    r = report(root, rules)
    if output == "json":
        typer.echo(json.dumps(r, indent=2))
        return
    console.print(
        f"[neon]architecture[/neon] {root.name}: {r['modules']} modules, {r['edges']} import "
        f"edges · {len(r['cycles'])} cycles · {len(r['violations'])} rule violations"
    )
    for c in r["cycles"][:5]:
        console.print("  cycle: " + " -> ".join(c[:8]) + (" …" if len(c) > 8 else ""))
    for v in r["violations"][:10]:
        console.print(f"  [danger]✗[/danger] {v}")
    if r["big_modules"]:
        console.print(
            "  biggest: " + ", ".join(f"{m} ({n} lines)" for m, n in r["big_modules"][:5])
        )
    console.print(
        "  most depended on: " + ", ".join(f"{m} ({n})" for m, n in r["most_depended_on"])
    )
    if r["cycles"] or r["violations"]:
        raise typer.Exit(code=1)


@app.command("mutate")
def mutate_cmd(
    files: list[Path] = typer.Argument(..., help="Source files to mutate."),
    project: Path = typer.Option(Path.cwd(), "--project", "-C"),
    function: list[str] = typer.Option([], "--function", "-f", help="Only these functions."),
    limit: int = typer.Option(20, "--limit", help="Mutants per file."),
    test_command: str | None = typer.Option(None, "--test", help="Default: the project's."),
    output: str = typer.Option("text", help="text | json"),
) -> None:
    """Mutation testing (U25): small code changes the tests should catch; survivors are behaviour
    no test pins down (missed edge cases). Files are always restored."""
    from trendlab.agent.mutation import run
    from trendlab.context.validation import detect_validation_commands
    from trendlab.engine.watch import project_env

    root = project.resolve()
    cmd = test_command or detect_validation_commands(root).get("test")
    if not cmd:
        raise typer.BadParameter("no test command found; pass --test")
    results = []
    for f in files:
        rel = str(f.resolve().relative_to(root)) if f.is_absolute() else str(f)
        results.append(
            run(root, rel, cmd, functions=set(function) or None, limit=limit, env=project_env(root))
        )
    if output == "json":
        typer.echo(json.dumps(results, indent=2))
        return
    for r in results:
        score = f"{r['score']:.0%}" if r["score"] is not None else "n/a"
        console.print(
            f"[neon]{r['file']}[/neon]: {r['killed']}/{r['mutants']} mutants killed ({score}) "
            f"in {r['seconds']}s"
        )
        for sv in r["survivors"]:
            console.print(f"  survived line {sv['line']}: {sv['kind']} {sv['change']}")


@app.command("surface")
def surface_cmd(
    project: Path = typer.Option(Path.cwd(), "--project", "-C"),
    output: str = typer.Option("text", help="text | json"),
) -> None:
    """Attack surface (U27): what the agent can reach under this config, and the risks."""
    from trendlab.config.loader import load_config
    from trendlab.security.surface import inventory, risks

    root = project.resolve()
    inv = inventory(load_config(root), root)
    found = risks(inv)
    if output == "json":
        typer.echo(json.dumps({**inv, "risks": found}, indent=2))
        return
    console.print(
        f"[neon]attack surface[/neon] · mode {inv['mode']} · {len(inv['tools'])} tools · "
        f"network {inv['network']['decision']} · {len(inv['mcp_servers'])} MCP servers · "
        f"{len(inv['hooks'])} hooks · secrets by name: {', '.join(inv['secrets_referenced'])}"
    )
    for r in found:
        console.print(f"  · {r}")


@app.command("audit")
def audit_cmd(project: Path = typer.Option(Path.cwd(), "--project", "-C")) -> None:
    """Dependency vulnerabilities (U27): the project venv's packages against OSV (sends package
    names and versions to api.osv.dev)."""
    from trendlab.security.surface import audit

    r = audit(project.resolve())
    if not r["vulnerable"]:
        console.print(f"[ok]no known vulnerabilities[/ok] in {r['packages']} packages")
        return
    console.print(f"[danger]{len(r['vulnerable'])} vulnerable[/danger] of {r['packages']}")
    for v in r["vulnerable"]:
        console.print(f"  {v['package']} {v['version']}: {', '.join(v['advisories'])}")
    raise typer.Exit(code=1)


@app.command("inbox")
def inbox_cmd(
    project: Path | None = typer.Option(None, "--project", "-C"),
    all_: bool = typer.Option(False, "--all", help="Every project, every status."),
) -> None:
    """List inbox cards (use /inbox inside a session to act on them)."""
    from trendlab.engine.daemon import inbox_path
    from trendlab.engine.inbox import Inbox

    inbox = Inbox(inbox_path())
    try:
        items = inbox.list(
            None if all_ else str((project or Path.cwd()).resolve()),
            status=None if all_ else "open",
        )
    finally:
        inbox.close()
    if not items:
        console.print("[dim]inbox empty[/dim]")
        return
    t = Table(title="Inbox")
    for col in ("id", "project", "status", "sev", "×", "title", "source"):
        t.add_column(col)
    for it in items:
        t.add_row(
            it["id"],
            Path(it["project"]).name,
            it["status"],
            it["severity"],
            str(it["occurrences"]),
            it["title"][:70],
            it["source"],
        )
    console.print(t)


@app.command("drift")
def drift_cmd(
    last: int = typer.Option(14, "--last", help="Nights to show."),
    distribution: bool = typer.Option(
        False, "--distribution", help="Compare real sessions with the suite (distribution shift)."
    ),
    days: int = typer.Option(30, "--days", help="Window of real sessions for --distribution."),
    periods: bool = typer.Option(
        False, "--periods", help="With --distribution: last N days vs the N days before."
    ),
) -> None:
    """Canary history per night, or how far real work is from what the suite measures."""
    if distribution:
        from trendlab.benchmarks.distribution import profile_sessions, profile_suite, shift_report
        from trendlab.sessions.store import SessionStore

        store = SessionStore(trendlab_home() / "sessions.db")
        try:
            if periods:
                from trendlab.benchmarks.distribution import period_report

                rep = period_report(store, days)
                rep["real_sessions"] = rep["periods"]["recent_sessions"]
            else:
                rep = shift_report(profile_sessions(store, days), profile_suite())
        finally:
            store.close()
        console.print(
            f"[neon]{rep['verdict']}[/neon] · {rep['real_sessions']} real sessions in {days} days"
        )
        for dim, d in rep["dimensions"].items():
            gaps = ", ".join(f"{k} {v:+.0%}" for k, v in d["biggest_gaps"])
            label = "recent − previous" if periods else "real − suite"
            console.print(f"  {dim}: distance {d['tv_distance']} · {label}: {gaps}")
        return
    hist = trendlab_home() / "engine" / "canary_history.jsonl"
    if not hist.is_file():
        console.print(
            "[dim]no canary history yet — enable [engine] canary = true or run "
            "`trendlab engine run canary`[/dim]"
        )
        return
    rows = [json.loads(ln) for ln in hist.read_text(encoding="utf-8").splitlines() if ln.strip()]
    t = Table(title="Canary drift")
    for col in ("night", "model", "served", "prompt", "passes", "ci95", "cost", "vs prev", "hints"):
        t.add_column(col)
    for r in rows[-last:]:
        vs = r.get("vs_previous") or {}
        t.add_row(
            str(r.get("at", ""))[:16],
            str(r.get("model", "")),
            ",".join(r.get("model_served") or []),
            ",".join(r.get("prompt_hash") or []),
            str(r.get("passes")),
            str(r.get("passes_ci95")),
            f"${r.get('cost', 0):.3f}",
            f"+{vs.get('b_wins', 0)}/-{vs.get('b_losses', 0)} p={vs.get('p_value', '')}"
            if vs
            else "",
            "; ".join(r.get("cause_hints") or [])[:60],
        )
    console.print(t)


@app.command("trace")
def trace_cmd(
    session_id: str = typer.Argument(..., help="Session id (see `trendlab sessions`)."),
    otel: Path | None = typer.Option(
        None, "--otel", help="Write an OpenTelemetry-style JSON file."
    ),
    depth: int = typer.Option(6, "--depth"),
) -> None:
    """Trace tree of a session: runs, model calls, tool calls, verify, with durations (U6)."""
    from trendlab.sessions.store import SessionStore
    from trendlab.telemetry.spans import render_tree, to_otel, trace_tree

    store = SessionStore(trendlab_home() / "sessions.db")
    try:
        records = [
            {"event": e["type"], "ts": e["ts"], "session_id": session_id, **(e.get("data") or {})}
            for e in store.events(session_id)
        ]
    finally:
        store.close()
    roots = trace_tree(records)
    if not roots:
        console.print(
            "[dim]no spans recorded for this session (sessions before v0.3.1 have none)[/dim]"
        )
        return
    from rich.markup import escape

    for line in render_tree(roots, max_depth=depth):
        console.print(escape(line))
    if otel is not None:
        otel.write_text(json.dumps(to_otel(session_id, roots), indent=1))
        console.print(f"[ok]wrote[/ok] {otel}")


@app.command("search")
def search_cmd(
    text: str = typer.Argument(..., help="Text to find in stored messages and events."),
    project: Path | None = typer.Option(None, "--project", "-C"),
    limit: int = typer.Option(30, "--limit"),
    ranked: bool = typer.Option(False, "--ranked", help="Rank whole sessions by relevance."),
    semantic: bool = typer.Option(
        False, "--semantic", help="Rank sessions by meaning ([context] embeddings or local)."
    ),
) -> None:
    """Search past sessions: exact matches, --ranked (BM25) or --semantic (embeddings)."""
    from trendlab.sessions.store import SessionStore

    store = SessionStore(trendlab_home() / "sessions.db")
    if semantic:
        from trendlab.config.loader import load_config
        from trendlab.context.vectors import make_embedder, semantic_sessions

        cfg = load_config()
        try:
            emb = make_embedder(cfg, cfg.context.embeddings or "ollama:nomic-embed-text")
            top = semantic_sessions(
                store, emb, text, cache=trendlab_home() / "index" / "sessions.json", k=limit
            )
        finally:
            store.close()
        t = Table(title=f"Sessions closest in meaning to: {text}")
        for col in ("score", "session", "updated", "project", "about"):
            t.add_column(col)
        for h in top:
            t.add_row(
                str(h["score"]),
                h["session"],
                str(h["updated_at"] or "")[:16],
                Path(h["project"] or "").name,
                h["preview"][:90],
            )
        console.print(t if top else "[dim]no sessions[/dim]")
        return
    if ranked:
        try:
            top = store.ranked_search(text, str(project.resolve()) if project else None, limit)
        finally:
            store.close()
        t = Table(title=f"Sessions most relevant to: {text}")
        for col in ("score", "session", "updated", "project", "first prompt"):
            t.add_column(col)
        for h in top:
            t.add_row(
                str(h["score"]),
                h["session_id"],
                h["ts"][:16],
                Path(h["project"] or "").name,
                h["first_prompt"][:90],
            )
        console.print(t if top else "[dim]no matches[/dim]")
        return
    try:
        hits = store.search(text, str(project.resolve()) if project else None, limit=limit)
    finally:
        store.close()
    if not hits:
        console.print("[dim]no matches[/dim]")
        return
    t = Table(title=f"Search: {text}")
    for col in ("when", "session", "project", "kind", "snippet"):
        t.add_column(col)
    for h in hits:
        t.add_row(
            h["ts"][:16],
            h["session_id"],
            Path(h["project"] or "").name,
            f"{h['kind']}:{h['role']}",
            h["snippet"][:110],
        )
    console.print(t)


@app.command("stats")
def stats_cmd(
    days: int = typer.Option(7, "--days"),
    output: str = typer.Option("text", help="text | json"),
) -> None:
    """Session analytics: volume, spend, outcomes, failure reasons, guards (U6)."""
    from trendlab.sessions.store import SessionStore

    store = SessionStore(trendlab_home() / "sessions.db")
    try:
        st = store.stats(days)
    finally:
        store.close()
    if output == "json":
        typer.echo(json.dumps(st, indent=2))
        return
    console.print(
        f"[neon]Last {days} days[/neon] · {st['sessions']} sessions in {st['projects']} projects · "
        f"{st['model_calls']} model calls · ${st['cost_usd']:.3f} · "
        f"{st['input_tokens']:,} in / {st['output_tokens']:,} out"
    )
    for label, key in (
        ("runs", "runs"),
        ("models", "models"),
        ("failure reasons", "failure_reasons"),
        ("guards fired", "guards"),
        ("skipped tools", "skipped_tools"),
        ("verifier verdicts", "verifier_verdicts"),
    ):
        if st[key]:
            console.print(f"  {label}: " + ", ".join(f"{k} ×{v}" for k, v in st[key].items()))
    if st["breakers_opened"]:
        console.print(f"  tool breakers opened: {st['breakers_opened']}")
    if st.get("routes"):
        console.print("  routes: " + ", ".join(f"{k} ×{v}" for k, v in st["routes"].items()))
    if st.get("communication"):
        c = st["communication"]
        console.print(
            f"  answers: {c['answers']} · median {c['median_words']} words · reading ease "
            f"{c['reading_ease']} · filler in {c['robospeak_rate']:.0%} · "
            f"{c['constraint_breaks']} broke a set rule"
        )
    oq = st.get("online_quality") or {}
    console.print(
        "  [neon]online quality[/neon]: "
        + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in oq.items() if v is not None)
    )


@app.command("import")
def import_cmd(
    paths: list[Path] = typer.Argument(..., help="Claude Code transcript files or directories."),
    since_days: int | None = typer.Option(
        None, "--since-days", help="Only files modified recently."
    ),
) -> None:
    """Import another harness's sessions so search, stats, trace and meta span both (U6)."""
    import time as _time

    from trendlab.config.loader import load_config
    from trendlab.providers.base import TokenUsage
    from trendlab.sessions.store import SessionStore
    from trendlab.telemetry.costs import CostTracker
    from trendlab.telemetry.importers import import_claude_code

    tracker = CostTracker(load_config())

    def price(model, inp, out, cached):
        return tracker.price(
            model, TokenUsage(input_tokens=inp, output_tokens=out, cached_input_tokens=cached)
        )

    files: list[Path] = []
    for p in paths:
        p = p.expanduser()
        files += sorted(p.rglob("*.jsonl")) if p.is_dir() else [p]
    if since_days is not None:
        cutoff = _time.time() - since_days * 86400
        files = [f for f in files if f.stat().st_mtime >= cutoff]
    store = SessionStore(trendlab_home() / "sessions.db")
    done = skipped = 0
    try:
        for f in files:
            try:
                r = import_claude_code(store, f, pricing=price)
            except Exception as exc:  # noqa: BLE001 — one bad file never stops the batch
                console.print(f"[warning]{f.name}: {exc}[/warning]")
                continue
            if r.get("skipped"):
                skipped += 1
            else:
                done += 1
                console.print(
                    f"  {r['file']} → {r['session']} · {r['messages']} msgs · "
                    f"{r['tool_calls']} tool calls · {r['model_calls']} model calls"
                )
    finally:
        store.close()
    console.print(f"[ok]imported {done}[/ok], skipped {skipped} (already imported or empty)")


def _load_rows(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "rows" in data:
        return [r for rows in data["rows"] for r in rows]
    return data if isinstance(data, list) else []


@app.command("label")
def label_cmd(
    rows_file: Path = typer.Argument(..., help="Rows saved with `bench --save-rows`."),
    labels_file: Path = typer.Option(Path("labels.json"), "--out"),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """Blind human grading: you see the task and the diff, not the config, verifier or test
    result. Afterwards reports agreement (Cohen's kappa) with the verifier and the hidden test."""
    import random

    from trendlab.benchmarks.stats import cohen_kappa

    rows = [r for r in _load_rows(rows_file) if r.get("diff")]
    labels = json.loads(labels_file.read_text()) if labels_file.is_file() else {}
    todo = [
        r
        for r in rows
        if f"{r.get('task')}#{r.get('config', r.get('profile'))}#{r.get('run', 1)}" not in labels
    ]
    random.Random(7).shuffle(todo)  # blind order: configs interleaved
    for r in todo[:limit]:
        key = f"{r.get('task')}#{r.get('config', r.get('profile'))}#{r.get('run', 1)}"
        console.rule()
        console.print(f"[neon]Task[/neon] {r.get('prompt', '')}")
        console.print(r["diff"][:4000], markup=False, highlight=False)
        ans = typer.prompt(
            "Correct and minimal? [y]es / [n]o / [s]kip / [q]uit", default="s"
        ).lower()[:1]
        if ans == "q":
            break
        if ans in {"y", "n"}:
            labels[key] = ans == "y"
            labels_file.write_text(json.dumps(labels, indent=1))
    human, judge, truth = [], [], []
    for r in rows:
        key = f"{r.get('task')}#{r.get('config', r.get('profile'))}#{r.get('run', 1)}"
        if key in labels:
            human.append(labels[key])
            judge.append(None if r.get("verification") is None else r.get("verification") == "pass")
            truth.append(bool(r.get("passes")))
    console.print(
        f"{len(human)} labelled · kappa human↔hidden test {cohen_kappa(human, truth)} · "
        f"human↔verifier {cohen_kappa(human, judge)} · "
        f"verifier↔hidden test {cohen_kappa(judge, truth)}"
    )


@app.command("judge-bias")
def judge_bias_cmd(
    rows_file: Path = typer.Argument(..., help="Rows saved with `bench --save-rows`."),
    limit: int = typer.Option(10, "--limit"),
    model: str | None = typer.Option(
        None, "--model", help="Verifier model (default: routing.verifier)."
    ),
) -> None:
    """Probe the verifier for verbosity and style bias: same change, three presentations."""
    from trendlab.agent.judge import bias_probe
    from trendlab.config.loader import load_config
    from trendlab.providers.gateway import ModelGateway
    from trendlab.telemetry.events import EventBus

    config = load_config()
    ref = (
        model
        or config.routing.get("verifier")
        or config.routing.get("escalation")
        or config.defaults.model
    )
    gw = ModelGateway(config, EventBus(), "judge-bias")

    async def call(messages):
        response, used = await gw.complete(ref, messages, None)
        return response.text, used

    rows = [r for r in _load_rows(rows_file) if r.get("diff")][:limit]

    async def run_all():
        out = []
        for r in rows:
            res = await bias_probe(call, task=r.get("prompt", ""), diff=r["diff"], validation=None)
            out.append(res)
            console.print(
                f"  {r.get('task')}: {res['plain']} / padded {res['padded']} "
                f"/ restyled {res['restyled']}"
            )
        return out

    results = asyncio.run(run_all())
    n = len(results) or 1
    vb = sum(1 for r in results if r["verbosity_bias"])
    sb = sum(1 for r in results if r["style_bias"])
    console.print(
        f"[neon]{ref}[/neon]: verbosity bias {vb}/{len(results)} ({vb / n:.0%}), "
        f"style bias {sb}/{len(results)} ({sb / n:.0%})"
    )


@app.command("dashboard")
def dashboard_cmd(
    out: Path = typer.Option(Path("trendlab-dashboard.html"), "--out"),
    days: int = typer.Option(14, "--days"),
) -> None:
    """One self-contained HTML page: sessions, spend, outcomes, tools, canary, inbox (U6)."""
    from trendlab.engine.daemon import inbox_path
    from trendlab.engine.inbox import Inbox
    from trendlab.sessions.store import SessionStore
    from trendlab.telemetry.dashboard import collect, render_html

    store = SessionStore(trendlab_home() / "sessions.db")
    inbox = Inbox(inbox_path())
    try:
        data = collect(store, days, trendlab_home() / "engine" / "canary_history.jsonl", inbox)
    finally:
        store.close()
        inbox.close()
    out.write_text(render_html(data), encoding="utf-8")
    console.print(f"[ok]dashboard[/ok] {out.resolve()}")


@app.command("export")
def export_cmd(
    out: Path = typer.Option(Path("trendlab-export"), "--out"),
    days: int | None = typer.Option(None, "--days"),
    messages: bool = typer.Option(False, "--messages", help="Also export message bodies."),
) -> None:
    """Export sessions, model calls and events as CSV / JSONL (warehouse feed)."""
    from trendlab.sessions.store import SessionStore
    from trendlab.telemetry.dashboard import export

    store = SessionStore(trendlab_home() / "sessions.db")
    try:
        counts = export(store, out, days=days, messages=messages)
    finally:
        store.close()
    console.print(
        "[ok]exported[/ok] " + ", ".join(f"{k} {v}" for k, v in counts.items()) + f" → {out}"
    )


@app.command("redteam")
def redteam_cmd(
    n: int = typer.Option(9, "--n", help="Attacks to generate."),
    model: str | None = typer.Option(
        None, "--model", help="Attack writer (default: routing.verifier)."
    ),
    run: bool = typer.Option(
        False, "--run", help="Also plant each attack and run the agent on it."
    ),
    agent_model: str = typer.Option("deepseek:deepseek-flash", "--agent-model"),
    out: Path | None = typer.Option(None, "--out", help="Save attacks and results as JSON."),
) -> None:
    """Generate new prompt-injection attacks; measure the scanner and (with --run) the agent."""
    from trendlab.benchmarks.redteam import generate, scanner_recall, task_for
    from trendlab.benchmarks.runner import run_task
    from trendlab.config.loader import load_config
    from trendlab.providers.gateway import ModelGateway
    from trendlab.telemetry.events import EventBus

    config = load_config()
    ref = model or config.routing.get("verifier") or config.defaults.model
    gw = ModelGateway(config, EventBus(), "redteam")

    async def call(messages):
        response, _used = await gw.complete(ref, messages, None)
        return response.text

    async def main():
        attacks = await generate(call, n)
        rec = scanner_recall(attacks)
        results = []
        if run:
            for i, a in enumerate(attacks, 1):
                r = await run_task(task_for(a, i), agent_model, profile="harness")
                results.append(
                    {
                        "goal": a["goal"],
                        "location": a["location"],
                        "safe": r.get("safe"),
                        "violations": r.get("violations"),
                        "passes": r.get("passes"),
                        "flagged": bool(r.get("interventions")),
                    }
                )
                console.print(
                    f"  {a['goal']:12s} {a['location']:12s} safe={r.get('safe')} "
                    f"fixed={r.get('passes')}"
                )
        return attacks, rec, results

    attacks, rec, results = asyncio.run(main())
    console.print(
        f"[neon]{len(attacks)} attacks[/neon] from {ref} · "
        f"scanner flagged {rec['flagged']} ({rec['recall']})"
    )
    for m in rec["missed"]:
        console.print(f"  [warning]missed:[/warning] {m}", markup=True)
    if results:
        safe = sum(1 for r in results if r["safe"])
        console.print(
            f"[neon]agent resisted {safe}/{len(results)}[/neon] "
            f"(task still fixed in {sum(1 for r in results if r['passes'])})"
        )
    if out:
        out.write_text(
            json.dumps(
                {"model": ref, "attacks": attacks, "scanner": rec, "runs": results}, indent=1
            )
        )


@app.command("doctor")
def doctor_cmd(project: Path = typer.Option(Path.cwd(), "--project", "-C")) -> None:
    """Check config, keys, providers, tools and remote settings; explain anything that is off."""
    import shutil
    import socket as _socket

    from trendlab.providers.registry import parse_model_ref
    from trendlab.security.secrets import secret_source

    ok, warn = "[ok]✓[/ok]", "[warning]![/warning]"
    rows: list[tuple[str, str, str]] = []
    try:
        config = load_config(project)
        rows.append((ok, "config", f"{global_config_path()} loads"))
    except ConfigError as exc:
        console.print(f"[danger]✗ config:[/danger] {exc}")
        raise typer.Exit(1) from exc
    try:
        provider, _ = parse_model_ref(config.defaults.model)
        pcfg = config.providers.get(provider)
        if pcfg is None:
            rows.append(
                (
                    warn,
                    "default model",
                    f"{config.defaults.model}: provider {provider!r} not configured",
                )
            )
        else:
            src = (
                "n/a (local)"
                if pcfg.type == "ollama" or not pcfg.api_key_env
                else secret_source(pcfg.api_key_env)
            )
            mark = ok if src != "missing" else warn
            rows.append(
                (
                    mark,
                    "default model",
                    f"{config.defaults.model} · key {pcfg.api_key_env or '-'}: {src}",
                )
            )
    except Exception as exc:  # noqa: BLE001
        rows.append((warn, "default model", str(exc)))
    for name, pcfg in config.providers.items():
        if pcfg.type == "ollama" or not pcfg.api_key_env:
            continue
        src = secret_source(pcfg.api_key_env)
        rows.append(
            (
                ok if src != "missing" else warn,
                f"provider {name}",
                f"{pcfg.type} · {pcfg.api_key_env}: {src}",
            )
        )
    rows.append((ok if shutil.which("git") else warn, "git", shutil.which("git") or "not found"))
    bwrap = shutil.which("bwrap")
    rows.append(
        (
            ok if (bwrap or config.sandbox.mode == "off") else warn,
            "sandbox",
            f"bubblewrap {bwrap} · mode {config.sandbox.mode}"
            if bwrap
            else f"bwrap not installed (mode {config.sandbox.mode}); apt install bubblewrap",
        )
    )
    rows.append(
        (
            ok if shutil.which("rg") else warn,
            "ripgrep",
            shutil.which("rg") or "not found (Python search fallback)",
        )
    )
    rows.append(
        (
            ok,
            "mode",
            f"{config.defaults.permission_mode.value}"
            + (
                " (AUTO: no prompts; --safe turns them on)"
                if config.defaults.permission_mode == PermissionMode.AUTO
                else ""
            ),
        )
    )
    roles = {"planning", "verifier", "escalation", "screener", "summarizer"}
    resolved = {config.routing.get(r, config.defaults.model) for r in roles}
    rows.append(
        (
            ok if len(resolved) > 1 else warn,
            "routing",
            ", ".join(f"{r}={config.routing[r]}" for r in sorted(config.routing))
            if len(resolved) > 1
            else "every role resolves to one model; set [routing] planning/verifier/escalation "
            "to a stronger model (trendlab init does this for DeepSeek)",
        )
    )
    ra = config.remote_approval
    if ra.enabled:
        try:
            with _socket.socket() as sock:
                sock.settimeout(0.5)
                busy = (
                    sock.connect_ex(
                        (ra.host if ra.host not in {"0.0.0.0", "::"} else "127.0.0.1", ra.port)
                    )
                    == 0
                )
            rows.append(
                (
                    warn if busy else ok,
                    "remote approval",
                    f"{ra.host}:{ra.port} " + ("port in use" if busy else "port free"),
                )
            )
        except OSError as exc:
            rows.append((warn, "remote approval", str(exc)))
    else:
        rows.append((ok, "remote approval", "disabled"))
    n = config.notifications
    rows.append((ok, "notifications", n.provider if n.enabled else "disabled"))
    rows.append((ok, "pricing entries", str(len(config.pricing))))
    t = Table(title="trendlab doctor", show_header=False)
    for mark, what, detail in rows:
        t.add_row(mark, what, detail)
    console.print(t)
    problems = sum(1 for r in rows if r[0] == warn)
    console.print(f"[dim]{problems} warning(s)[/dim]" if problems else "[ok]all checks passed[/ok]")


@app.command("init")
def init_cmd(
    provider: str = typer.Option(
        None, help="deepseek | anthropic | openai | ollama (prompted if omitted)"
    ),
    model: str | None = typer.Option(
        None, help="Model id for that provider (defaults per provider)."
    ),
) -> None:
    """First-run setup: pick a provider, store its key safely, write ~/.trendlab/config.toml."""
    from trendlab.security.secrets import secret_source, store_secret

    presets = {
        "deepseek": (
            "openai_compatible",
            "https://api.deepseek.com/v1",
            "DEEPSEEK_API_KEY",
            "deepseek-flash",
        ),
        "anthropic": ("anthropic", "", "ANTHROPIC_API_KEY", "claude-opus-5"),
        "openai": (
            "openai_compatible",
            "https://api.openai.com/v1",
            "OPENAI_API_KEY",
            "gpt-4o-mini",
        ),
        "moonshot": (
            "openai_compatible",
            "https://api.moonshot.ai/v1",
            "MOONSHOT_API_KEY",
            "kimi-k2",
        ),
        "ollama": ("ollama", "http://localhost:11434/v1", None, "qwen3-coder"),
    }
    if provider is None:
        console.print(f"[neon]Welcome to {PRODUCT_NAME}.[/neon] Choose a provider:")
        for i, name in enumerate(presets, start=1):
            console.print(f"  {i}. {name}")
        choice = typer.prompt("Provider", default="1")
        provider = (
            list(presets)[int(choice) - 1]
            if choice.isdigit() and 1 <= int(choice) <= len(presets)
            else choice
        )
    if provider not in presets:
        console.print(
            f"[danger]unknown provider {provider!r}[/danger]; choose from {', '.join(presets)}"
        )
        raise typer.Exit(2)
    ptype, base_url, key_env, default_model = presets[provider]
    model = model or default_model
    if key_env and secret_source(key_env) == "missing":
        value = typer.prompt(
            f"Paste your {provider} API key for {key_env} (input hidden)", hide_input=True
        )
        store_secret(key_env, value)
        console.print(f"[ok]Stored {key_env}[/ok] in ~/.trendlab/secrets (never displayed).")
    values = {"type": ptype, "api_key_env": key_env} if key_env else {"type": ptype}
    if base_url:
        values["base_url"] = base_url
    update_global_config("providers", {provider: values})
    update_global_config("defaults", {"model": f"{provider}:{model}"})
    if provider == "deepseek":
        # Model cocktail (cheap-model spec §6.1): Flash leads, V4 Pro plans/verifies/escalates.
        update_global_config(
            "routing",
            {
                "planning": "deepseek:deepseek-v4-pro",
                "verifier": "deepseek:deepseek-v4-pro",
                "escalation": "deepseek:deepseek-v4-pro",
                "screener": "deepseek:deepseek-flash",
                "summarizer": "deepseek:deepseek-flash",
            },
        )
        console.print(
            "[ok]Routing[/ok] planning/verifier/escalation → V4 Pro; screener/summarizer → Flash"
        )
    console.print(
        f"[ok]Configured[/ok] default model [neon]{provider}:{model}[/neon] "
        f"in {global_config_path()}"
    )
    console.print(
        "Next: [neon]trendlab doctor[/neon] to check everything, then [neon]trendlab[/neon] "
        "in a project."
    )


def main() -> None:
    app()


pr_app = typer.Typer(help="Pull-request triage, throughput and workspaces (U21; read-only gh).")
app.add_typer(pr_app, name="pr")


@pr_app.command("list")
def pr_list_cmd(
    repo: str | None = typer.Option(None, "--repo", help="owner/name (default: this repo)."),
    project: Path = typer.Option(Path.cwd(), "--project", "-C"),
    limit: int = typer.Option(50, "--limit"),
    output: str = typer.Option("text", help="text | json"),
) -> None:
    """Triage open PRs: size, CI, review state, idle time and the next action for each."""
    from trendlab.orchestration.prs import list_open, triage

    rows = triage(list_open(repo, project.resolve(), limit))
    if output == "json":
        typer.echo(json.dumps(rows, indent=2))
        return
    t = Table(title=f"Open PRs · {repo or project.resolve().name}")
    for col in ("#", "action", "size", "CI", "review", "idle", "title"):
        t.add_column(col)
    for r in rows:
        t.add_row(
            str(r["number"]),
            r["action"] + (f" ({r['note']})" if r["note"] else ""),
            f"{r['size']} {r['lines']}",
            r["ci"],
            r["review"].lower().replace("_", " "),
            f"{r['idle_days']}d",
            r["title"][:60],
        )
    console.print(t if rows else "[dim]no open pull requests[/dim]")


@pr_app.command("stats")
def pr_stats_cmd(
    repo: str | None = typer.Option(None, "--repo"),
    project: Path = typer.Option(Path.cwd(), "--project", "-C"),
    days: int = typer.Option(30, "--days"),
) -> None:
    """PR throughput: merged per week, hours to merge (median, p90), size merged."""
    from trendlab.orchestration.prs import list_merged, throughput

    typer.echo(json.dumps(throughput(list_merged(repo, project.resolve(), days), days=days)))


@pr_app.command("workspace")
def pr_workspace_cmd(
    number: int = typer.Argument(..., help="PR number."),
    project: Path = typer.Option(Path.cwd(), "--project", "-C"),
    prompt: str | None = typer.Option(
        None, "--agent", help="Run the agent in the PR worktree with this prompt."
    ),
) -> None:
    """Check a PR out into its own worktree (current checkout untouched); optionally run the
    agent there. Review it with `trendlab review -C <worktree>`."""
    from trendlab.orchestration.prs import workspace

    dest = workspace(project.resolve(), number)
    console.print(f"[ok]PR #{number}[/ok] in {dest}")
    if prompt:
        import subprocess

        rc = subprocess.run([sys.executable, "-m", "trendlab.cli", "-C", str(dest), "-p", prompt])
        raise typer.Exit(code=rc.returncode)


if __name__ == "__main__":
    main()
