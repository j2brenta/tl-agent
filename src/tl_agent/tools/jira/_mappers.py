"""Envelope → domain mappers.

Jira wraps issue data in a `fields` envelope and exposes story points via a
per-instance custom field; the mappers below translate that raw JSON into our
narrow domain models. Sprint day/length are *not* Jira concepts — they're
derived downstream from the sprint's start/end dates (see
`phases/_sprint.py`).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, cast

from tl_agent.models import JiraSprint, JiraSprintState, JiraStatus, JiraStatusChange, JiraTicket
from tl_agent.settings import get_settings

_CATEGORY_BUCKET: dict[str, JiraStatus] = {
    "new": JiraStatus.TODO,
    "indeterminate": JiraStatus.IN_PROGRESS,
    "done": JiraStatus.DONE,
}


def parse_dt(raw: str) -> datetime:
    """Parse a Jira timestamp, tolerating `Z` and colon-less offsets (`+0000`)."""
    txt = raw.strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(txt)
    except ValueError:
        if len(txt) >= 5 and txt[-5] in "+-" and txt[-3] != ":":
            txt = txt[:-2] + ":" + txt[-2:]
        return datetime.fromisoformat(txt)


def _bucket_from_name(name: str | None) -> JiraStatus | None:
    if not name:
        return None
    mapped = get_settings().jira_status_map.get(name.strip().lower())
    return JiraStatus(mapped) if mapped else None


def _status_bucket(status: dict[str, Any] | str | None) -> JiraStatus:
    """Map a Jira status (object or bare name) to our coarse bucket.

    Prefers the configured name map; falls back to `statusCategory.key`.
    """
    if isinstance(status, str):
        return _bucket_from_name(status) or JiraStatus.TODO
    if not status:
        return JiraStatus.TODO
    by_name = _bucket_from_name(status.get("name"))
    if by_name is not None:
        return by_name
    category: dict[str, Any] = status.get("statusCategory") or {}
    return _CATEGORY_BUCKET.get(str(category.get("key", "")), JiraStatus.TODO)


def links_from_fields(
    issuelinks: list[dict[str, Any]] | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Extract (blocks, blocked_by) keys from `fields.issuelinks`.

    Only "Blocks" links count; `outwardIssue` ⇒ this issue blocks it,
    `inwardIssue` ⇒ this issue is blocked by it. Other link types (relates
    to, duplicates, …) are ignored.
    """
    blocks: list[str] = []
    blocked_by: list[str] = []
    links: list[dict[str, Any]] = issuelinks or []
    for link in links:
        ltype: dict[str, Any] = link.get("type") or {}
        name = str(ltype.get("name", "")).strip().lower()
        outward = str(ltype.get("outward", "")).strip().lower()
        if name != "blocks" and outward != "blocks":
            continue
        out_issue: dict[str, Any] = link.get("outwardIssue") or {}
        in_issue: dict[str, Any] = link.get("inwardIssue") or {}
        out_key = out_issue.get("key")
        in_key = in_issue.get("key")
        if out_key:
            blocks.append(str(out_key))
        if in_key:
            blocked_by.append(str(in_key))
    return tuple(blocks), tuple(blocked_by)


def _sprint_name(val: Any) -> str | None:
    """Best-effort sprint name from the (instance-specific) sprint field."""
    if isinstance(val, list):
        items = cast("list[object]", val)
        return _sprint_name(items[-1]) if items else None
    if isinstance(val, dict):
        name = cast("dict[str, Any]", val).get("name")
        return str(name) if name is not None else None
    if isinstance(val, str):
        m = re.search(r"name=([^,\]]+)", val)
        return m.group(1) if m else None
    return None


def _person(obj: dict[str, Any]) -> str | None:
    """Best identity for a Jira user object.

    Prefer the human-readable `displayName`; fall back to the Server/DC
    `name` (username), then the Cloud `accountId`. Whichever we surface is
    later folded onto the roster via `TeamConfig.resolve()`, which matches on
    `display_name`, `jira_account_id`, and `aliases` — so configuring an
    engineer's account id or display name both resolve.
    """
    return obj.get("displayName") or obj.get("name") or obj.get("accountId")


def ticket_from_issue(issue: dict[str, Any]) -> JiraTicket:
    s = get_settings()
    fields: dict[str, Any] = issue.get("fields") or {}
    assignee: dict[str, Any] = fields.get("assignee") or {}
    reporter: dict[str, Any] = fields.get("reporter") or {}
    points = fields.get(s.jira_points_field)
    sprint_name = _sprint_name(fields.get(s.jira_sprint_field)) if s.jira_sprint_field else None
    blocks, blocked_by = links_from_fields(fields.get("issuelinks"))
    labels: list[Any] = fields.get("labels") or []
    return JiraTicket(
        key=issue["key"],
        summary=fields.get("summary") or "",
        status=_status_bucket(fields.get("status")),
        assignee=_person(assignee),
        reporter=_person(reporter),
        points=float(points) if points is not None else None,
        sprint=sprint_name,
        created_at=parse_dt(fields["created"]),
        updated_at=parse_dt(fields["updated"]),
        labels=tuple(str(label) for label in labels),
        blocks=blocks,
        blocked_by=blocked_by,
    )


def status_changes(values: list[dict[str, Any]]) -> list[JiraStatusChange]:
    """Flatten changelog `values[].items[]` to our status-only transitions."""
    changes: list[JiraStatusChange] = []
    for entry in values:
        at_raw = entry.get("created")
        if not at_raw:
            continue
        at = parse_dt(at_raw)
        author: dict[str, Any] = entry.get("author") or {}
        by = author.get("displayName") or author.get("name")
        items: list[dict[str, Any]] = entry.get("items") or []
        for item in items:
            if str(item.get("field", "")).lower() != "status":
                continue
            changes.append(
                JiraStatusChange(
                    at=at,
                    by=by,
                    from_status=_status_bucket(item.get("fromString")),
                    to_status=_status_bucket(item.get("toString")),
                )
            )
    return changes


def sprint_from_meta(meta: dict[str, Any], *, board_id: str | None = None) -> JiraSprint:
    origin = meta.get("originBoardId") or meta.get("board_id") or board_id
    return JiraSprint(
        id=str(meta["id"]),
        name=meta.get("name") or str(meta["id"]),
        state=JiraSprintState(str(meta.get("state", "active")).lower()),
        board_id=str(origin) if origin is not None else None,
        start_date=parse_dt(meta["startDate"]) if meta.get("startDate") else None,
        end_date=parse_dt(meta["endDate"]) if meta.get("endDate") else None,
    )
