"""FastAPI app for Phase 8 review.

Minimal — three routes: /brief (queue + each card), /decisions (audit log),
POST /decisions/{id}/{action}. HTMX swaps the card into "approved" state
inline; the backend invokes `phase8_execute.execute_decision()`.

Why FastAPI + HTMX rather than a SPA: zero build tooling, the markup is
inspectable in the rendered HTML, and Phase 8 is a 4-button surface — a
React app would be overkill.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse

from tl_agent.obs.tracing import init_tracing
from tl_agent.web.routes import decisions as decisions_route
from tl_agent.web.routes import review as review_route
from tl_agent.web.routes import runs as runs_route
from tl_agent.web.routes import sprint as sprint_route
from tl_agent.web.routes import team as team_route
from tl_agent.web.routes import workflow as workflow_route

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Process-wide startup/shutdown — sets up OTel and ensures the schema.

    The schema-ensure mirrors `phases.orchestrator.run`: it makes the web UI
    self-healing on a fresh deployment (e.g. the containerised path, where the
    bind-mounted `data/` starts empty) instead of 500ing with "no such table".
    `initialize` is idempotent — schema.sql is all `CREATE … IF NOT EXISTS`.
    """
    from tl_agent.settings import get_settings
    from tl_agent.storage import connect, initialize

    init_tracing(service_name="tl-agent-web")

    db_path = get_settings().sqlite_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        initialize(conn)
    finally:
        conn.close()

    yield


app = FastAPI(title="tl-agent-review", version="0.1.0", lifespan=_lifespan)


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    return HTMLResponse(
        '<meta http-equiv="refresh" content="0; url=/brief">'
        '<a href="/brief">view today\'s brief</a>'
    )


# Wire the routers
app.include_router(review_route.router)
app.include_router(decisions_route.router)
app.include_router(sprint_route.router)
app.include_router(runs_route.router)
app.include_router(team_route.router)
app.include_router(workflow_route.router)


@app.post("/jira/import", response_class=HTMLResponse)
async def jira_import(date: str | None = Form(None)) -> HTMLResponse:
    """Pull the active sprint from Jira and save a snapshot for the given date."""
    from datetime import date as _date

    from tl_agent.storage import connect, transaction
    from tl_agent.storage.repos import snapshots as snapshots_repo
    from tl_agent.tools.base import ToolError
    from tl_agent.tools.jira import ListSprintIn, ListSprintTool

    run_date_iso = date or _date.today().isoformat()
    try:
        run_date = _date.fromisoformat(run_date_iso)
    except ValueError:
        return HTMLResponse(
            '<div id="jira-status" class="banner banner-warn">⚠ Invalid date.</div>'
        )

    try:
        tool = ListSprintTool()
        outcome = await tool.invoke(ListSprintIn().model_dump(), run_date_iso=run_date_iso)
        if isinstance(outcome, ToolError):
            raise RuntimeError(outcome.message)
        result = outcome.value
    except Exception as exc:
        logger.warning("jira/import failed: %s", exc)
        return HTMLResponse(
            f'<div id="jira-status" class="banner banner-warn">⚠ Jira fetch failed: {exc}</div>'
        )

    from tl_agent.settings import get_settings

    conn = connect(get_settings().sqlite_path)
    with transaction(conn):
        for ticket in result.tickets:
            snapshots_repo.upsert(conn, run_date, ticket)

    response = HTMLResponse(
        f'<div id="jira-status" class="banner banner-ok">'
        f"✓ Imported {len(result.tickets)} tickets from {result.sprint_id}. Reloading…"
        f"</div>"
    )
    response.headers["HX-Redirect"] = f"/sprint?date={run_date_iso}"
    return response


@app.post("/standup/import", response_class=HTMLResponse)
async def standup_import(date: str | None = Form(None)) -> HTMLResponse:
    """Pull today's standups from Mattermost and persist to standup_observations."""
    from datetime import UTC, datetime, timedelta
    from datetime import date as _date

    from tl_agent.phases.phase1_collect import fetch_standups
    from tl_agent.storage import connect, transaction
    from tl_agent.storage.markdown_loader import load_team
    from tl_agent.storage.repos import observations as obs_repo

    run_date_iso = date or _date.today().isoformat()
    try:
        run_date = _date.fromisoformat(run_date_iso)
    except ValueError:
        return HTMLResponse(
            '<div id="standup-status" class="banner banner-warn">⚠ Invalid date.</div>'
        )

    # Build a minimal context so fetch_standups can resolve engineer names.
    team = load_team()

    class _MinCtx:
        standup_channel_id = "town-square"
        team = None

        def __init__(self) -> None:
            self.notes: list[str] = []

    ctx = _MinCtx()
    ctx.team = team  # type: ignore[assignment]

    since = datetime(run_date.year, run_date.month, run_date.day, 0, 0, tzinfo=UTC)
    until = since + timedelta(days=1)

    try:
        messages = await fetch_standups(ctx, since=since, until=until)  # type: ignore[arg-type]
    except Exception as exc:
        logger.warning("standup/import failed: %s", exc)
        banner = '<div id="standup-status" class="banner banner-warn">'
        banner += f"⚠ Mattermost fetch failed: {exc}</div>"
        return HTMLResponse(banner)

    if not messages:
        return HTMLResponse(
            f'<div id="standup-status" class="banner banner-warn">'
            f"⚠ No standup messages found in town-square for {run_date_iso}.</div>"
        )

    from tl_agent.settings import get_settings

    conn = connect(get_settings().sqlite_path)
    with transaction(conn):
        for msg in messages:
            obs_repo.upsert(
                conn,
                obs_id=f"{run_date_iso}:{msg.engineer_id}",
                run_date=run_date,
                engineer_id=msg.engineer_id,
                raw=msg.raw,
                summary=None,
                chat_message_id=msg.chat_message_id,
            )

    response = HTMLResponse(
        f'<div id="standup-status" class="banner banner-ok">'
        f"✓ Imported {len(messages)} standup(s) for {run_date_iso}. Reloading…</div>"
    )
    response.headers["HX-Redirect"] = f"/sprint?date={run_date_iso}"
    return response


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "today": date.today().isoformat()}
