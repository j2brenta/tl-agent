"""Sprint dashboard — per-engineer status, standups, tickets, flags.

Route:
  GET /sprint   — latest run date by default; ?date=YYYY-MM-DD to pick another
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from tl_agent.models import FlagType, JiraStatus
from tl_agent.storage import connect
from tl_agent.storage.markdown_loader import load_team
from tl_agent.storage.repos import flags as flags_repo
from tl_agent.storage.repos import observations as obs_repo
from tl_agent.storage.repos import snapshots as snapshots_repo
from tl_agent.storage.repos import standup_segments as segments_repo
from tl_agent.web import _dates

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=select_autoescape())


def _conn() -> sqlite3.Connection:
    from tl_agent.settings import get_settings

    return connect(get_settings().sqlite_path)


def _list_snapshot_dates(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT run_date FROM ticket_snapshots ORDER BY run_date DESC LIMIT 30"
    ).fetchall()
    return [r["run_date"] for r in rows]


def _validate_date(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        return None


@router.get("/sprint", response_class=HTMLResponse)
async def sprint(request: Request, date: str | None = None) -> HTMLResponse:
    conn = _conn()
    selected = _validate_date(date) or _dates.cookie_date(request)
    available = _list_snapshot_dates(conn)

    if selected is None and available:
        selected = available[0]

    template = _env.get_template("sprint.html")

    if selected is None:
        return HTMLResponse(
            template.render(
                no_data=True,
                selected_date=None,
                available_dates=available,
            )
        )

    run_date = _date_from_iso(selected)
    tickets = snapshots_repo.list_for_date(conn, run_date)
    team = load_team()
    open_flags = flags_repo.list_open_on(conn, run_date)

    # Index flags by engineer
    flags_by_eng: dict[str, list[Any]] = {}
    off_sprint_flags: list[Any] = []
    team_flags: list[Any] = []
    for flag in open_flags:
        if flag.type == FlagType.OFF_SPRINT:
            off_sprint_flags.append(flag)
        elif flag.engineer_id:
            flags_by_eng.setdefault(flag.engineer_id, []).append(flag)
        else:
            team_flags.append(flag)

    # Build per-engineer rows
    engineers_data: list[dict[str, Any]] = []
    for eng in team.engineers:
        eng_tickets = [t for t in tickets if t.assignee == eng.id]
        eng_flags = flags_by_eng.get(eng.id, [])
        observation = obs_repo.get(conn, run_date=run_date, engineer_id=eng.id)
        segments = segments_repo.list_for_engineer_date(
            conn, engineer_id=eng.id, date_iso=run_date.isoformat()
        )

        blocked = [t for t in eng_tickets if t.status == JiraStatus.BLOCKED]
        done = [t for t in eng_tickets if t.status == JiraStatus.DONE]
        hot_flags = [f for f in eng_flags if f.days_hot >= 2]
        has_data = bool(eng_tickets or observation or eng_flags)
        on_track = has_data and not blocked and not hot_flags

        engineers_data.append(
            {
                "engineer": eng,
                "tickets": eng_tickets,
                "flags": eng_flags,
                "observation": observation,
                "segments": segments,
                "on_track": on_track,
                "blocked_count": len(blocked),
                "done_count": len(done),
            }
        )

    # Sprint-level aggregates
    sprint_name = next((t.sprint for t in tickets if t.sprint), None)
    total = len(tickets)
    done_total = sum(1 for t in tickets if t.status == JiraStatus.DONE)
    blocked_total = sum(1 for t in tickets if t.status == JiraStatus.BLOCKED)
    in_progress_total = sum(1 for t in tickets if t.status == JiraStatus.IN_PROGRESS)
    in_review_total = sum(1 for t in tickets if t.status == JiraStatus.IN_REVIEW)
    planned_pts = sum(t.points or 0 for t in tickets)
    done_pts = sum(t.points or 0 for t in tickets if t.status == JiraStatus.DONE)

    # Unplanned / mid-sprint additions: tickets added_to_sprint_at set
    unplanned_tickets = [t for t in tickets if t.added_to_sprint_at is not None]

    response = HTMLResponse(
        template.render(
            selected_date=selected,
            available_dates=available,
            sprint_name=sprint_name,
            engineers=engineers_data,
            team_flags=team_flags,
            off_sprint_flags=off_sprint_flags,
            unplanned_tickets=unplanned_tickets,
            total=total,
            done_total=done_total,
            blocked_total=blocked_total,
            in_progress_total=in_progress_total,
            in_review_total=in_review_total,
            planned_pts=planned_pts,
            done_pts=done_pts,
            no_data=False,
        )
    )
    _dates.set_date_cookie(response, selected)
    return response


def _date_from_iso(s: str) -> date:
    return date.fromisoformat(s)
