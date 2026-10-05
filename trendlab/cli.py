"""Top-level ``trendlab`` command."""

from __future__ import annotations

import asyncio
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
console = Console()


def _version(value: bool) -> None:
    if value:
        typer.echo(f"{PRODUCT_NAME} {__version__}")
        raise typer.Exit()


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
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    try:
        config = load_config(project)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    from trendlab.app import TrendLabApp
    from trendlab.ui.repl import Repl

    tl_app = TrendLabApp(project, config, model_ref=model, console=console, permission_mode=mode)
    if prompt:
        asyncio.run(_run_once(tl_app, prompt))
    else:
        asyncio.run(Repl(tl_app).run())


async def _run_once(tl_app, prompt: str) -> None:
    await tl_app.start(interactive=True)
    try:
        reply = await tl_app.agent.run(prompt)
        console.print(reply)
    finally:
        await tl_app.stop()


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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
