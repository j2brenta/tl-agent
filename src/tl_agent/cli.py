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
) -> None:
    """Run the full 8-phase tech-lead loop for the given date."""
    from tl_agent.phases.orchestrator import run as orch_run

    target = date_cls.fromisoformat(run_date) if run_date else date_cls.today()
    console.print(f"[bold]tl-agent[/bold] running for {target.isoformat()}")
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
