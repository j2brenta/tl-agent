"""Run history — phase-by-phase details for a given date."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import date as date_
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=select_autoescape())


def _conn() -> sqlite3.Connection:
    from tl_agent.settings import get_settings
    from tl_agent.storage import connect

    return connect(get_settings().sqlite_path)


@router.get("/runs/fragment", response_class=HTMLResponse)
async def runs_fragment(date: str | None = None) -> HTMLResponse:
    """HTMX fragment: run details panel for a given date."""
    selected: str | None = None
    if date:
        with contextlib.suppress(ValueError):
            selected = date_.fromisoformat(date).isoformat()

    if not selected:
        selected = date_.today().isoformat()

    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM runs WHERE run_date = ? ORDER BY started_at DESC",
        (selected,),
    ).fetchall()

    runs: list[dict[str, Any]] = []
    for r in rows:
        notes: dict[str, Any] = {}
        if r["notes"]:
            with contextlib.suppress(Exception):
                notes = json.loads(r["notes"])
        runs.append(
            {
                "id": r["id"],
                "run_date": r["run_date"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "status": r["status"],
                "phases": notes.get("phases", []),
                "errors": notes.get("errors", []),
                "signals": notes.get("signals", {}),
            }
        )

    template = _env.get_template("_runs_fragment.html")
    return HTMLResponse(template.render(runs=runs, selected_date=selected))
