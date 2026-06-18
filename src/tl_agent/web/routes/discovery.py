"""GitLab discovery & coverage — what the morning loop looked at, and who
pushed that we don't recognise.

Route:
  GET /discovery  — for the selected date's commit window, show the GitLab
                    group(s) discovered, a per-project coverage table (was it
                    searched, how many commits, any error), and the
                    "unconfigured contributors" list: commit authors that don't
                    resolve to anyone on the roster (config/team.md).

This is the GitLab mirror of the Team tab's identity-mapping view. Phase 1
fetches every commit per project and buckets authors via `team.resolve()`
(`phases.phase1_collect.fetch_commits`); the leftover authors surface here so
an off-team push to a team repo — or a roster member missing their
`email`/`gitlab_username` — doesn't go unnoticed.
"""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import date as date_
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from tl_agent.phases.phase1_collect import fetch_commits, gitlab_commit_window
from tl_agent.storage import load_team

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=select_autoescape())


def _conn() -> sqlite3.Connection:
    from tl_agent.settings import get_settings
    from tl_agent.storage import connect

    return connect(get_settings().sqlite_path)


def _coerce_date(date: str | None) -> str:
    if date:
        with contextlib.suppress(ValueError):
            return date_.fromisoformat(date).isoformat()
    return date_.today().isoformat()


def _available_dates(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT run_date FROM runs ORDER BY run_date DESC LIMIT 30"
    ).fetchall()
    return [r["run_date"] for r in rows]


@router.get("/discovery", response_class=HTMLResponse)
async def discovery(date: str | None = None) -> HTMLResponse:
    selected = _coerce_date(date)
    team = load_team()
    since, until = gitlab_commit_window(date_.fromisoformat(selected), team)

    notes: list[str] = []
    commits, manifest = await fetch_commits(team, since, until, selected, notes)

    conn = _conn()
    try:
        available_dates = _available_dates(conn)
    finally:
        conn.close()

    template = _env.get_template("discovery.html")
    return HTMLResponse(
        template.render(
            selected_date=selected,
            available_dates=available_dates,
            since=since.isoformat(),
            until=until.isoformat(),
            manifest=manifest,
            team_commit_count=len(commits),
            error="; ".join(notes) or None,
        )
    )
