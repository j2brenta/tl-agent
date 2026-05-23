"""Phase 8 review queue + per-decision approve/reject/edit.

Routes:
  GET  /brief                          — pending queue
  POST /decisions/{id}/approve         — execute + readback
  POST /decisions/{id}/reject          — persist rejection only
  POST /decisions/{id}/edit            — edit body + approve
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from tl_agent.models import ApprovalAction
from tl_agent.phases.phase8_execute import execute_decision
from tl_agent.storage import connect
from tl_agent.storage.repos import decisions as decisions_repo
from tl_agent.tools import SqliteIdempotencyStore

logger = logging.getLogger(__name__)
router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=select_autoescape())


def _conn() -> object:
    from tl_agent.settings import get_settings

    return connect(get_settings().sqlite_path)


@router.get("/brief", response_class=HTMLResponse)
async def brief() -> HTMLResponse:
    conn = _conn()
    pending = decisions_repo.list_pending(conn)  # type: ignore[arg-type]
    template = _env.get_template("brief.html")
    return HTMLResponse(template.render(pending=pending, today=date.today().isoformat()))


@router.post("/decisions/{decision_id}/approve", response_class=HTMLResponse)
async def approve(decision_id: str) -> HTMLResponse:
    return await _act(decision_id, ApprovalAction.APPROVED, edited_body=None)


@router.post("/decisions/{decision_id}/reject", response_class=HTMLResponse)
async def reject(decision_id: str) -> HTMLResponse:
    return await _act(decision_id, ApprovalAction.REJECTED, edited_body=None)


@router.post("/decisions/{decision_id}/edit", response_class=HTMLResponse)
async def edit(decision_id: str, body: str = Form(...)) -> HTMLResponse:
    return await _act(decision_id, ApprovalAction.EDITED, edited_body=body)


async def _act(
    decision_id: str, action: ApprovalAction, *, edited_body: str | None
) -> HTMLResponse:
    from tl_agent.settings import get_settings

    conn = connect(get_settings().sqlite_path)
    store = SqliteIdempotencyStore(conn)
    try:
        updated = await execute_decision(
            conn=conn,
            idempotency=store,
            decision_id=decision_id,
            action=action,
            edited_body=edited_body,
            edited_target=None,
            run_date_iso=date.today().isoformat(),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    template = _env.get_template("_decision_card.html")
    return HTMLResponse(template.render(decision=updated))
