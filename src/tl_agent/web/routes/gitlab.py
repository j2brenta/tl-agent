"""Gitlab — the team's discovered projects and their cached commits.

Routes:
  GET  /gitlab          — page: projects from the discovery registry
                          (`gitlab_projects`, warmed by the startup discovery
                          pass) as collapsed accordions, each showing the
                          commits cached for the selected day. Renders instantly
                          off local storage — no live GitLab call.
  POST /gitlab/refresh  — re-pull the GitLab commit window for the selected day
                          from GitLab, persist it (`collected_commits` +
                          `collection_state`), and swap the refreshed fragment.

This is the GitLab mirror of the Team tab's identity-mapping view: commits are
fetched per project and bucketed via `team.resolve()`; authors that resolve to
nobody surface under "Unconfigured contributors".
"""

from __future__ import annotations

import sqlite3
from datetime import date as date_
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from tl_agent.phases.phase1_collect import fetch_commits, gitlab_commit_window
from tl_agent.storage import load_team
from tl_agent.storage.repos import collection_state, gitlab_projects
from tl_agent.storage.repos import commits as commits_repo
from tl_agent.web import _dates

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=select_autoescape())


def _conn() -> sqlite3.Connection:
    from tl_agent.settings import get_settings
    from tl_agent.storage import connect

    return connect(get_settings().sqlite_path)


def _fragment_context(conn: sqlite3.Connection, selected: str) -> dict[str, object]:
    """Assemble the registry + cached-commit view for the selected day."""
    run_date = date_.fromisoformat(selected)
    cached = commits_repo.list_for_date(conn, run_date)
    by_project: dict[str, list[object]] = {}
    for c in cached:
        by_project.setdefault(c.project, []).append(c)

    registry = gitlab_projects.list_active(conn)
    # Registry projects first (with their cached commits), then any project that
    # has commits cached but isn't in the registry (e.g. a fallback-list repo).
    registry_paths = {p.path for p in registry}
    projects = [
        {"path": p.path, "group_path": p.group_path, "commits": by_project.get(p.path, [])}
        for p in registry
    ]
    for path, cs in sorted(by_project.items()):
        if path not in registry_paths:
            projects.append({"path": path, "group_path": "", "commits": cs})

    state = collection_state.get(conn, run_date)
    manifest = state.manifest if state else None
    return {
        "selected_date": selected,
        "projects": projects,
        "manifest": manifest,
        "team_commit_count": len(cached),
        "gitlab_collected_at": state.gitlab_collected_at if state else None,
    }


@router.get("/gitlab", response_class=HTMLResponse)
async def gitlab(request: Request, date: str | None = None) -> HTMLResponse:
    selected = _dates.resolve_date(request, date)
    conn = _conn()
    try:
        ctx = _fragment_context(conn, selected)
        available = _dates.available_dates(conn)
    finally:
        conn.close()
    template = _env.get_template("gitlab.html")
    response = HTMLResponse(template.render(available_dates=available, **ctx))
    _dates.set_date_cookie(response, selected)
    return response


@router.post("/gitlab/refresh", response_class=HTMLResponse)
async def gitlab_refresh(request: Request, date: str | None = Form(None)) -> HTMLResponse:
    """Re-pull the commit window from GitLab and update the local cache."""
    selected = _dates.resolve_date(request, date)
    run_date = date_.fromisoformat(selected)
    team = load_team()
    since, until = gitlab_commit_window(run_date, team)

    conn = _conn()
    try:
        notes: list[str] = []
        commits, manifest = await fetch_commits(team, since, until, selected, notes, conn=conn)
        commits_repo.replace_for_date(conn, run_date, commits)
        collection_state.set_gitlab(conn, run_date, manifest=manifest, commits_count=len(commits))
        conn.commit()
        ctx = _fragment_context(conn, selected)
    finally:
        conn.close()

    template = _env.get_template("_gitlab_fragment.html")
    return HTMLResponse(template.render(error="; ".join(notes) or None, **ctx))
