"""Minimal Jira REST subset for tl-agent.

Endpoints (only what the agent uses):
  GET  /rest/api/3/health
  GET  /rest/api/3/issue/{key}
  GET  /rest/api/3/issue/{key}/changelog
  GET  /rest/api/3/issue/{key}/links
  GET  /rest/agile/1.0/board/{board_id}/sprint
  GET  /rest/agile/1.0/sprint/active/issue
  GET  /rest/agile/1.0/sprint/{sprint_id}/issue
  POST /rest/api/3/issue/{key}/comment

State lives in memory + a JSON fixtures file. `seed.py` (run-once) writes
the fixtures; this server reads them on startup. Reset = `make restart`.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

FIXTURES = Path(__file__).parent / "fixtures" / "state.json"


def _load_state() -> dict[str, Any]:
    if not FIXTURES.exists():
        return {
            "tickets": {},
            "history": {},
            "links": {},
            "sprint": {
                "sprint_id": "S-2026-05",
                "sprint_day": 1,
                "sprint_length_days": 10,
                "added_since": None,
                "issues": [],
            },
            "board_id": "ENG",
            "sprints": [
                {
                    "id": "S-2026-05",
                    "name": "Eng Sprint 19",
                    "state": "active",
                    "board_id": "ENG",
                    "start_date": None,
                    "end_date": None,
                    "sprint_day": 1,
                    "sprint_length_days": 10,
                }
            ],
        }
    loaded: dict[str, Any] = json.loads(FIXTURES.read_text(encoding="utf-8"))
    return loaded


state: dict[str, Any] = _load_state()
app = FastAPI(title="jira_mock", version="0.1.0")


@app.get("/rest/api/3/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/rest/api/3/issue/{key}")
def get_issue(key: str) -> dict[str, Any]:
    issue: dict[str, Any] | None = state["tickets"].get(key)
    if not issue:
        raise HTTPException(status_code=404, detail=f"not found: {key}")
    return issue


@app.get("/rest/api/3/issue/{key}/changelog")
def get_changelog(key: str) -> dict[str, Any]:
    if key not in state["tickets"]:
        raise HTTPException(status_code=404, detail=f"not found: {key}")
    return {"values": state["history"].get(key, [])}


@app.get("/rest/api/3/issue/{key}/links")
def get_links(key: str) -> dict[str, Any]:
    if key not in state["tickets"]:
        raise HTTPException(status_code=404, detail=f"not found: {key}")
    links: dict[str, Any] = state["links"].get(key, {"blocks": [], "blocked_by": []})
    return links


@app.get("/rest/agile/1.0/board/{board_id}/sprint")
def get_board_sprints(board_id: str) -> dict[str, Any]:
    """List the sprints on a board (metadata only — no issues)."""
    if state.get("board_id") and state["board_id"] != board_id:
        raise HTTPException(status_code=404, detail=f"board not found: {board_id}")
    return {"values": state.get("sprints", [])}


@app.get("/rest/agile/1.0/sprint/active/issue")
def get_active_sprint() -> dict[str, Any]:
    sprint: dict[str, Any] = state["sprint"]
    return sprint


@app.get("/rest/agile/1.0/sprint/{sprint_id}/issue")
def get_sprint(sprint_id: str) -> dict[str, Any]:
    active: dict[str, Any] = state["sprint"]
    if active.get("sprint_id") == sprint_id:
        return active
    # A non-active sprint chosen by a human: synthesize a ticket-list payload
    # from the board metadata. The seed only stocks the active sprint with
    # issues, so others come back empty but well-shaped.
    meta = next((s for s in state.get("sprints", []) if s.get("id") == sprint_id), None)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"sprint not found: {sprint_id}")
    return {
        "sprint_id": meta["id"],
        "sprint_day": meta.get("sprint_day") or 1,
        "sprint_length_days": meta.get("sprint_length_days") or 10,
        "added_since": None,
        "issues": [],
    }


@app.post("/rest/api/3/issue/{key}/comment")
def post_comment(key: str, payload: dict[str, str]) -> dict[str, Any]:
    if key not in state["tickets"]:
        raise HTTPException(status_code=404, detail=f"not found: {key}")
    body = payload.get("body", "")
    comment_id = f"C-{uuid.uuid4().hex[:8]}"
    return {
        "key": key,
        "comment_id": comment_id,
        "posted_at": datetime.now(UTC).isoformat(),
        "body": body,
    }


# Dev convenience — `python -m services.jira_mock.app` for ad-hoc local runs
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("JIRA_MOCK_PORT", "9100"))
    uvicorn.run(app, host="0.0.0.0", port=port)  # noqa: S104 — local dev only
