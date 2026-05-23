"""Minimal Jira REST subset for tl-agent.

Endpoints (only what the agent uses):
  GET  /rest/api/3/health
  GET  /rest/api/3/issue/{key}
  GET  /rest/api/3/issue/{key}/changelog
  GET  /rest/api/3/issue/{key}/links
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


@app.get("/rest/agile/1.0/sprint/active/issue")
def get_active_sprint() -> dict[str, Any]:
    sprint: dict[str, Any] = state["sprint"]
    return sprint


@app.get("/rest/agile/1.0/sprint/{sprint_id}/issue")
def get_sprint(sprint_id: str) -> dict[str, Any]:
    sprint: dict[str, Any] = state["sprint"]
    if sprint.get("sprint_id") != sprint_id:
        raise HTTPException(status_code=404, detail=f"sprint not found: {sprint_id}")
    return sprint


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
