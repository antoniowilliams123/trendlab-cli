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
) -> None:
    """Run the benchmark fixtures A–E, the 50-task suite, or a two-configuration comparison."""
    from trendlab.benchmarks.runner import (
        CANARY,
        append_bench_log,
        compare_rows,
        run_benchmarks,
        run_suite,
        summarize,
    )

    if canary:
        suite, tasks, tier = True, CANARY, "all"
    if suite or compare:
        configs = compare if compare else [f"{model}@{profile}"]
        if compare and len(configs) != 2:
            raise typer.BadParameter("--compare needs exactly two model@profile values")
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
                )
            )
            summary = {"config": spec, **summarize(results)}
            summaries.append(summary)
            all_rows.append(results)
            if log:
                append_bench_log(Path("docs/BENCH_LOG.md"), f"suite {spec}", results)
        if output == "json":
            typer.echo(json.dumps({"summaries": summaries}, indent=2))
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
        if len(summaries) == 2:
            cmp = compare_rows(all_rows[0], all_rows[1])
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
    session_id: str = typer.Argument(..., help="Stored session id (see `trendlab sessions`)."),
    model: str | None = typer.Option(
        None, "--model", "-m", help="Model for the replay (default: the session's)."
    ),
    max_prompts: int | None = typer.Option(None, "--max-prompts"),
    output: str = typer.Option("text", help="text | json"),
) -> None:
    """Shadow replay: re-run a stored session's prompts against the current build (spec §8.6)."""
    from trendlab.benchmarks.replay import replay_session
    from trendlab.config.loader import load_config, trendlab_home
    from trendlab.sessions.store import SessionStore

    config = load_config()
    store = SessionStore(trendlab_home() / "sessions.db")
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
def drift_cmd(last: int = typer.Option(14, "--last", help="Nights to show.")) -> None:
    """Canary history: pass rate, cost, served model id and prompt hash per night (U3)."""
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


if __name__ == "__main__":
    main()
