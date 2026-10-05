"""Top-level ``trendlab`` command."""

from __future__ import annotations

import asyncio
import json
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
console = Console()


def _version(value: bool) -> None:
    if value:
        typer.echo(f"{PRODUCT_NAME} {__version__}")
        raise typer.Exit()


def _first_run_check(config, model_ref: str) -> None:
    """Spec §56: a useful message instead of a stack trace when nothing is configured."""
    import os

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
    if pcfg.type != "ollama" and pcfg.api_key_env and not os.environ.get(pcfg.api_key_env):
        console.print(
            f"[yellow]Welcome to {PRODUCT_NAME}.[/yellow] Set [bold]{pcfg.api_key_env}[/bold] in "
            f"your environment for provider {provider!r} (secrets are never stored in config)."
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
    from trendlab.app import TrendLabApp

    if prompt:
        _first_run_check(config, model or config.defaults.model)
        tl_app = TrendLabApp(
            project, config, model_ref=model, console=console, permission_mode=mode, resume=resume
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
        run_tui(project, config, model_ref=model, permission_mode=mode, resume=resume)
        return
    tl_app = TrendLabApp(
        project,
        config,
        model_ref=model,
        console=console,
        permission_mode=mode,
        resume=resume,
        on_token=lambda t: console.print(t, end="", highlight=False),
    )
    from trendlab.ui.repl import Repl

    repl = Repl(tl_app)
    asyncio.run(_run_repl(repl))


async def _run_repl(repl) -> None:
    loop = asyncio.get_running_loop()
    presses = {"n": 0}

    def on_sigint() -> None:
        presses["n"] += 1
        if repl.cancel_current():
            console.print("\n[yellow]Canceling current task… (Ctrl+C again to quit)[/yellow]")
            return
        if presses["n"] >= 2 or True:
            console.print("\n[dim]bye[/dim]")
            repl._quit()  # noqa: SLF001
            loop.call_soon(
                lambda: [
                    t.cancel()
                    for t in asyncio.all_tasks(loop)
                    if t is not asyncio.current_task(loop)
                ]
            )

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
) -> None:
    """Run the autonomous coding benchmark fixtures against a model (spec §54)."""
    from trendlab.benchmarks.runner import run_benchmarks

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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
