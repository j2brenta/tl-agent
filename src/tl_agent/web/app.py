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


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "today": date.today().isoformat()}
