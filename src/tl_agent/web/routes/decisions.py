"""Audit log view — every decision the TL has acted on."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from tl_agent.storage import connect
from tl_agent.storage.repos import decisions as decisions_repo

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=select_autoescape())


@router.get("/decisions", response_class=HTMLResponse)
async def decisions_log(date: str | None = None) -> HTMLResponse:
    from datetime import date as _date

    from tl_agent.settings import get_settings

    conn = connect(get_settings().sqlite_path)
    selected: str | None = None
    if date:
        try:
            selected = _date.fromisoformat(date).isoformat()
        except ValueError:
            selected = None
    recent = decisions_repo.list_recent(conn, limit=200, run_date=selected)
    available = decisions_repo.list_run_dates(conn)
    template = _env.get_template("decisions.html")
    return HTMLResponse(
        template.render(
            decisions=recent,
            selected_date=selected,
            available_dates=available,
        )
    )
