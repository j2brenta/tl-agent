"""Minimal Jira REST subset for tl-agent — emits *real-shaped* Jira JSON.

The internal fixture state (written by `seed.py`) is kept in a convenient
flat form; each handler *renders* it into the envelope a real Jira would
return, so the tools exercise the same parsing they'd use against a live
instance.

Endpoints (only what the agent uses):
  GET  /rest/api/{2,3}/health
  GET  /rest/api/{2,3}/issue/{key}                  → {key, fields:{…}}
  GET  /rest/api/{2,3}/issue/{key}/changelog        → {values:[…], isLast}
  POST /rest/api/{2,3}/issue/{key}/comment          (ADF or plain body)
  GET  /rest/agile/1.0/board/{board_id}/sprint      → {values:[…]}
  GET  /rest/agile/1.0/sprint/{sprint_id}           → sprint metadata
  GET  /rest/agile/1.0/sprint/{sprint_id}/issue     → {issues:[…]}

Both core REST versions (2 = Server/DC, 3 = Cloud) resolve via the `{ver}`
path param. State lives in memory + a JSON fixtures file. `seed.py`
(run-once) writes the fixtures; this server reads them on startup.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Query

FIXTURES = Path(__file__).parent / "fixtures" / "state.json"

# Story points live in an instance-specific custom field; this id matches the
# `jira_points_field` default in tl_agent.settings.
POINTS_FIELD = "customfield_10016"

# Internal status bucket → (display name, statusCategory key) as a real Jira
# instance would report it.
_STATUS_RENDER: dict[str, tuple[str, str]] = {
    "todo": ("To Do", "new"),
    "in_progress": ("In Progress", "indeterminate"),
    "in_review": ("In Review", "indeterminate"),
    "blocked": ("Blocked", "indeterminate"),
    "done": ("Done", "done"),
}


def _load_state() -> dict[str, Any]:
    if not FIXTURES.exists():
        return {
            "tickets": {},
            "history": {},
            "links": {},
            "sprint": {"sprint_id": "S-2026-05", "issues": []},
            "board_id": "ENG",
            "sprints": [
                {
                    "id": "S-2026-05",
                    "name": "Eng Sprint 19",
                    "state": "active",
                    "board_id": "ENG",
                    "start_date": None,
                    "end_date": None,
                }
            ],
        }
    loaded: dict[str, Any] = json.loads(FIXTURES.read_text(encoding="utf-8"))
    return loaded


state: dict[str, Any] = _load_state()
app = FastAPI(title="jira_mock", version="0.2.0")


# -------------------- renderers (internal → real Jira shape) --------------------


def _render_issuelinks(key: str) -> list[dict[str, Any]]:
    entry: dict[str, Any] = state["links"].get(key, {})
    blocks_type = {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"}
    downstream_keys: list[Any] = entry.get("blocks", [])
    upstream_keys: list[Any] = entry.get("blocked_by", [])
    links: list[dict[str, Any]] = [
        {"type": blocks_type, "outwardIssue": {"key": d}} for d in downstream_keys
    ]
    links += [{"type": blocks_type, "inwardIssue": {"key": u}} for u in upstream_keys]
    return links


def _render_issue(t: dict[str, Any]) -> dict[str, Any]:
    name, category = _STATUS_RENDER.get(t.get("status", "todo"), ("To Do", "new"))
    fields: dict[str, Any] = {
        "summary": t.get("summary"),
        "status": {"name": name, "statusCategory": {"key": category}},
        "assignee": {"displayName": t["assignee"]} if t.get("assignee") else None,
        "reporter": {"displayName": t["reporter"]} if t.get("reporter") else None,
        "created": t.get("created_at"),
        "updated": t.get("updated_at"),
        "labels": t.get("labels", []),
        POINTS_FIELD: t.get("points"),
        "issuelinks": _render_issuelinks(t["key"]),
    }
    return {"key": t["key"], "fields": fields}


def _render_changelog(key: str) -> dict[str, Any]:
    values: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = state["history"].get(key, [])
    for h in history:
        from_name = _STATUS_RENDER.get(h.get("from_status", "todo"), ("To Do", "new"))[0]
        to_name = _STATUS_RENDER.get(h.get("to_status", "todo"), ("To Do", "new"))[0]
        values.append(
            {
                "created": h["at"],
                "author": {"displayName": h.get("by")},
                "items": [{"field": "status", "fromString": from_name, "toString": to_name}],
            }
        )
    return _page(values)


def _render_sprint_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": meta["id"],
        "name": meta.get("name"),
        "state": meta.get("state"),
        "originBoardId": meta.get("board_id"),
        "startDate": meta.get("start_date"),
        "endDate": meta.get("end_date"),
    }


def _page(items: list[dict[str, Any]], *, key: str = "values") -> dict[str, Any]:
    return {
        key: items,
        "startAt": 0,
        "maxResults": max(len(items), 1),
        "total": len(items),
        "isLast": True,
    }


def _find_sprint(sprint_id: str) -> dict[str, Any] | None:
    sprints: list[dict[str, Any]] = state.get("sprints", [])
    return next((s for s in sprints if str(s.get("id")) == sprint_id), None)


def _node_text(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    node_d = cast("dict[str, Any]", node)
    return str(node_d.get("text", "")) if node_d.get("type") == "text" else None


def _flatten_adf(body: Any) -> str:
    """Reduce an ADF doc (v3) or plain string (v2) to its text."""
    if not isinstance(body, dict):
        return str(body or "")
    parts: list[str] = []
    blocks: list[Any] = cast("dict[str, Any]", body).get("content", [])
    for block in blocks:
        if not isinstance(block, dict):
            continue
        nodes: list[Any] = cast("dict[str, Any]", block).get("content", [])
        parts.extend(text for text in map(_node_text, nodes) if text is not None)
    return "".join(parts)


# -------------------- core REST (v2 + v3) --------------------


@app.get("/rest/api/{ver}/health")
def health(ver: str) -> dict[str, str]:
    return {"status": "ok"}


@app.get("/rest/api/{ver}/issue/{key}")
def get_issue(ver: str, key: str) -> dict[str, Any]:
    t: dict[str, Any] | None = state["tickets"].get(key)
    if not t:
        raise HTTPException(status_code=404, detail=f"not found: {key}")
    return _render_issue(t)


@app.get("/rest/api/{ver}/issue/{key}/changelog")
def get_changelog(ver: str, key: str) -> dict[str, Any]:
    if key not in state["tickets"]:
        raise HTTPException(status_code=404, detail=f"not found: {key}")
    return _render_changelog(key)


@app.post("/rest/api/{ver}/issue/{key}/comment")
def post_comment(ver: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    if key not in state["tickets"]:
        raise HTTPException(status_code=404, detail=f"not found: {key}")
    now = datetime.now(UTC).isoformat()
    return {
        "id": f"C-{uuid.uuid4().hex[:8]}",
        "author": {"displayName": "tl-agent"},
        "body": payload.get("body"),
        "renderedBody": _flatten_adf(payload.get("body")),
        "created": now,
        "updated": now,
    }


# -------------------- Agile API (version-independent) --------------------


@app.get("/rest/agile/1.0/board/{board_id}/sprint")
def get_board_sprints(
    board_id: str, sprint_state: str | None = Query(default=None, alias="state")
) -> dict[str, Any]:
    """List the sprints on a board (metadata only — no issues)."""
    if state.get("board_id") and state["board_id"] != board_id:
        raise HTTPException(status_code=404, detail=f"board not found: {board_id}")
    sprints: list[dict[str, Any]] = state.get("sprints", [])
    if sprint_state:
        sprints = [s for s in sprints if s.get("state") == sprint_state]
    return _page([_render_sprint_meta(s) for s in sprints])


@app.get("/rest/agile/1.0/sprint/{sprint_id}")
def get_sprint_meta(sprint_id: str) -> dict[str, Any]:
    meta = _find_sprint(sprint_id)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"sprint not found: {sprint_id}")
    return _render_sprint_meta(meta)


@app.get("/rest/agile/1.0/sprint/{sprint_id}/issue")
def get_sprint_issues(sprint_id: str) -> dict[str, Any]:
    meta = _find_sprint(sprint_id)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"sprint not found: {sprint_id}")
    # The seed only stocks the active sprint with issues; others come back
    # empty but well-shaped.
    active: dict[str, Any] = state["sprint"]
    raw_issues: list[dict[str, Any]] = (
        active.get("issues", []) if str(active.get("sprint_id")) == sprint_id else []
    )
    return _page([_render_issue(t) for t in raw_issues], key="issues")


# Dev convenience — `python -m services.jira_mock.app` for ad-hoc local runs
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("JIRA_MOCK_PORT", "9100"))
    uvicorn.run(app, host="0.0.0.0", port=port)  # noqa: S104 — local dev only
