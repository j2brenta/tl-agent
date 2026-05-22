"""tl-agent CLI entry point.

Subcommands wire to the orchestrator; everything heavy lives in `phases/`.
The CLI itself stays thin so the same code paths run under tests and the web UI.
"""

from __future__ import annotations

from datetime import date as date_cls
from typing import Annotated

import typer
from rich.console import Console

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
    target = date_cls.fromisoformat(run_date) if run_date else date_cls.today()
    console.print(f"[bold]tl-agent[/bold] running for {target.isoformat()}")
    console.print("[dim]orchestrator not wired yet — see phases/orchestrator.py[/dim]")
    # TODO(orchestrator): wire up `phases.orchestrator.run(target)`.


if __name__ == "__main__":
    app()
