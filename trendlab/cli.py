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
    kept = "sudo and paths outside the project are still denied"
    destructive = (
        "destructive commands (rm -rf, git reset --hard, force-push) run WITHOUT asking"
        if allow_destructive
        else "destructive commands still ask at the terminal"
    )
    return (
        f"[bold white on red] UNSAFE MODE [/bold white on red] [red]permissions are skipped for "
        f"this session: edits, shell, installs, network and deletes run without approval; "
        f"{destructive}; "
        f"{kept}. Every auto-approved operation is logged; /undo restores the pre-edit checkpoint. "
        f'Turn prompts on with --safe, /mode ask, or permission_mode = "ask" in config.[/red]'
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
        "--dangerously-skip-permissions",
        help="UNSAFE (the default): run edits, shell, installs, network and deletes without "
        "asking. "
        "sudo and paths outside the project stay denied; destructive commands still ask unless "
        "--allow-destructive. Use --safe to turn prompts on for this session.",
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
        "--allow-destructive",
        help="In unsafe mode: also run rm -rf / destructive git without asking.",
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
            "[red]--allow-destructive only applies in unsafe mode "
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
    _update_hint()
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
                " (UNSAFE: no prompts; --safe turns them on)"
                if config.defaults.permission_mode.value == "unsafe"
                else ""
            ),
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
