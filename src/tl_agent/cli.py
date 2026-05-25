"""tl-agent CLI entry point.

Subcommands wire to the orchestrator; everything heavy lives in `phases/`.
The CLI itself stays thin so the same code paths run under tests and the web UI.
"""

from __future__ import annotations

import asyncio
from datetime import date as date_cls
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from tl_agent import __version__

app = typer.Typer(
    name="tl-agent",
    help="Tech-lead agentic workflow",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.command()
def version() -> None:
    """Print the package version."""
    console.print(f"tl-agent {__version__}")


@app.command()
def run(
    run_date: Annotated[
        str,
        typer.Option("--date", help="ISO date for the run (defaults to today)"),
    ] = "",
    verbose: Annotated[
        bool,
        typer.Option("--verbose/--quiet", help="Stream phase progress + tool calls to stderr"),
    ] = True,
) -> None:
    """Run the full 8-phase tech-lead loop for the given date."""
    import logging

    from tl_agent.phases.orchestrator import run as orch_run

    if verbose:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-5s %(name)s — %(message)s",
            datefmt="%H:%M:%S",
        )
        # Mute noisy third-party loggers; keep our own + warnings + the OTLP
        # exporter (so "Failed to export span batch" still surfaces).
        for noisy in ("httpx", "httpcore", "urllib3", "anthropic", "openai"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    target = date_cls.fromisoformat(run_date) if run_date else date_cls.today()
    console.print(f"[bold]tl-agent[/bold] running for {target.isoformat()}")
    _print_router_summary()
    result = asyncio.run(orch_run(target))

    table = Table(title=f"Run {result.run_id} — {result.run_date.isoformat()}")
    table.add_column("metric")
    table.add_column("value")
    table.add_row("decisions drafted", str(len(result.brief.decisions)))
    table.add_row("deep-dives", str(result.deep_dives_count))
    table.add_row("open flags", str(result.open_flag_count))
    table.add_row("closed flags", str(result.closed_flag_count))
    console.print(table)

    for d in result.brief.decisions:
        console.print(
            f"  [yellow]{d.proposed_mode.value}[/yellow] {d.hotspot_id}: {d.proposed_body[:160]}"
        )

    if result.notes:
        console.print("\n[dim]notes:[/dim]")
        for n in result.notes:
            console.print(f"  - {n}")


def _print_router_summary() -> None:
    """Show which provider + model each phase will use, plus relevant env knobs.

    Loads the same router the orchestrator will use, so the CLI banner is
    authoritative — no risk of drift between what we print and what runs.
    """
    from tl_agent.llm.router import build_default
    from tl_agent.settings import get_settings

    settings = get_settings()
    router = build_default()
    cfg_path = router.config_path
    cfg_label = (
        cfg_path.relative_to(settings.repo_root)
        if cfg_path is not None and cfg_path.is_absolute()
        else cfg_path
    )

    console.print(f"[dim]router config:[/dim] {cfg_label}")
    providers_in_use = {r.provider for r in router.routes.values()}
    if "ollama" in providers_in_use:
        console.print(
            f"[dim]ollama:[/dim] base_url={settings.ollama_base_url} "
            f"timeout={settings.ollama_timeout_seconds:.0f}s"
        )
    if "anthropic" in providers_in_use:
        key_state = "set" if settings.anthropic_api_key else "[red]EMPTY[/red]"
        console.print(f"[dim]anthropic:[/dim] api_key={key_state}")

    table = Table(title="Router")
    table.add_column("route")
    table.add_column("provider")
    table.add_column("model")
    table.add_column("max_tokens", justify="right")
    table.add_column("temp", justify="right")
    table.add_column("cache_sys", justify="right")
    for name in sorted(router.routes):
        r = router.routes[name]
        table.add_row(
            name,
            r.provider,
            r.model,
            str(r.max_tokens),
            f"{r.temperature:.1f}",
            "y" if r.cache_system else "n",
        )
    console.print(table)


@app.command(name="init-db")
def init_db(
    db_path: Annotated[
        str,
        typer.Option("--path", help="override DB path (defaults to settings.sqlite_path)"),
    ] = "",
) -> None:
    """Apply schema.sql to the SQLite DB, creating it if missing (idempotent)."""
    from pathlib import Path

    from tl_agent.settings import get_settings
    from tl_agent.storage.db import connect, initialize

    target = Path(db_path) if db_path else get_settings().sqlite_path
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(target)
    try:
        initialize(conn)
    finally:
        conn.close()
    console.print(f"[green]initialised SQLite at {target}[/green]")


@app.command()
def reset(
    confirm: Annotated[
        bool,
        typer.Option("--confirm", help="required — refuses to run without it"),
    ] = False,
    db_path: Annotated[
        str,
        typer.Option("--path", help="override DB path (defaults to settings.sqlite_path)"),
    ] = "",
) -> None:
    """Delete the SQLite state file and re-apply schema.sql."""
    from pathlib import Path

    from tl_agent.settings import get_settings
    from tl_agent.storage.db import connect, initialize

    if not confirm:
        console.print("[red]refusing to reset without --confirm[/red]")
        raise typer.Exit(code=2)

    target = Path(db_path) if db_path else get_settings().sqlite_path
    if target.exists():
        target.unlink()
        console.print(f"deleted {target}")
    else:
        console.print(f"[dim]no db at {target} — creating fresh[/dim]")

    target.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(target)
    try:
        initialize(conn)
    finally:
        conn.close()
    console.print(f"[green]re-applied schema at {target}[/green]")


if __name__ == "__main__":
    app()
