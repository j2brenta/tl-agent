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

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from tl_agent.obs.tracing import init_tracing
from tl_agent.web.routes import decisions as decisions_route
from tl_agent.web.routes import review as review_route
from tl_agent.web.routes import sprint as sprint_route

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Process-wide startup/shutdown — sets up OTel exactly once."""
    init_tracing(service_name="tl-agent-web")
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


@app.post("/jira/import", response_class=HTMLResponse)
async def jira_import(date: str | None = None) -> HTMLResponse:
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
async def standup_import(date: str | None = None) -> HTMLResponse:
    """Stub — returns a banner telling the user this isn't wired yet."""
    return HTMLResponse(
        '<div id="standup-status" class="banner banner-warn">'
        "⚠ Mattermost import is not yet wired — run "
        "<code>tl-agent run</code> to populate standup data."
        "</div>"
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "today": date.today().isoformat()}
