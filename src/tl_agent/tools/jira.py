"""Jira tools.

Calls the real Jira REST API. In compose we point at the in-process
`services/jira_mock` (a FastAPI subset that now emits *real-shaped* Jira
JSON); the same code talks to a live Jira given the right `jira_base_url` +
token in settings.

Two Jira surfaces are in play:
- **Core REST** (`/rest/api/{2,3}`) for issues, changelog, comments. v3 is
  Jira Cloud (ADF comment bodies); v2 is Server/Data Center (plain text).
  Only the version prefix + comment serialization differ — see `_api_base`
  and `_comment_payload`. The version comes from `settings.jira_api_version`.
- **Agile API** (`/rest/agile/1.0`) for boards + sprints. This surface is
  version-independent (orthogonal to v2/v3).

Jira wraps issue data in a `fields` envelope and exposes story points via a
per-instance custom field; the `_*_from_*` mappers translate that raw JSON
into our narrow domain models. Sprint day/length are *not* Jira concepts —
they're derived downstream from the sprint's start/end dates (see
`phases/_sprint.py`).

Tools registered here:
- get_ticket (read)
- get_ticket_history (read)
- get_ticket_dependencies (read)
- list_sprints (read) — board sprint discovery
- list_sprint (read) — tickets in a sprint
- post_jira_comment (WRITE — requires_approval=True; Phase 5 excluded)

All inputs/outputs are Pydantic models; HTTP errors → typed ToolExceptions.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, ClassVar, cast

import httpx
from pydantic import BaseModel, Field

from tl_agent.models import JiraSprint, JiraSprintState, JiraStatus, JiraStatusChange, JiraTicket
from tl_agent.settings import get_settings
from tl_agent.tools._http import http_client, raise_from_http_error, raise_from_transport_error
from tl_agent.tools.base import BaseTool, RetryPolicy, ToolErrorKind, ToolException
from tl_agent.tools.idempotency import make_key
from tl_agent.tools.registry import registry


def _client() -> httpx.AsyncClient:
    s = get_settings()
    return http_client(
        base_url=s.jira_base_url,
        headers={"Authorization": f"Bearer {s.jira_token}", "Accept": "application/json"},
    )


def _api_base() -> str:
    """Core REST prefix for the configured Jira flavor (`/rest/api/2|3`)."""
    return f"/rest/api/{get_settings().jira_api_version}"


def _comment_payload(body: str) -> dict[str, Any]:
    """v3 (Cloud) requires ADF; v2 (Server/DC) takes a plain string."""
    if get_settings().jira_api_version == "2":
        return {"body": body}
    return {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": body}]}],
        }
    }


# -------------------- envelope → domain mappers --------------------

_CATEGORY_BUCKET: dict[str, JiraStatus] = {
    "new": JiraStatus.TODO,
    "indeterminate": JiraStatus.IN_PROGRESS,
    "done": JiraStatus.DONE,
}


def _dt(raw: str) -> datetime:
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


def _links_from_fields(
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


def _ticket_from_issue(issue: dict[str, Any]) -> JiraTicket:
    s = get_settings()
    fields: dict[str, Any] = issue.get("fields") or {}
    assignee: dict[str, Any] = fields.get("assignee") or {}
    reporter: dict[str, Any] = fields.get("reporter") or {}
    points = fields.get(s.jira_points_field)
    sprint_name = _sprint_name(fields.get(s.jira_sprint_field)) if s.jira_sprint_field else None
    blocks, blocked_by = _links_from_fields(fields.get("issuelinks"))
    labels: list[Any] = fields.get("labels") or []
    return JiraTicket(
        key=issue["key"],
        summary=fields.get("summary") or "",
        status=_status_bucket(fields.get("status")),
        assignee=_person(assignee),
        reporter=_person(reporter),
        points=float(points) if points is not None else None,
        sprint=sprint_name,
        created_at=_dt(fields["created"]),
        updated_at=_dt(fields["updated"]),
        labels=tuple(str(label) for label in labels),
        blocks=blocks,
        blocked_by=blocked_by,
    )


def _status_changes(values: list[dict[str, Any]]) -> list[JiraStatusChange]:
    """Flatten changelog `values[].items[]` to our status-only transitions."""
    changes: list[JiraStatusChange] = []
    for entry in values:
        at_raw = entry.get("created")
        if not at_raw:
            continue
        at = _dt(at_raw)
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


def _sprint_from_meta(meta: dict[str, Any], *, board_id: str | None = None) -> JiraSprint:
    origin = meta.get("originBoardId") or meta.get("board_id") or board_id
    return JiraSprint(
        id=str(meta["id"]),
        name=meta.get("name") or str(meta["id"]),
        state=JiraSprintState(str(meta.get("state", "active")).lower()),
        board_id=str(origin) if origin is not None else None,
        start_date=_dt(meta["startDate"]) if meta.get("startDate") else None,
        end_date=_dt(meta["endDate"]) if meta.get("endDate") else None,
    )


async def _paginate(
    client: httpx.AsyncClient,
    path: str,
    items_key: str,
    tool_label: str,
    *,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Walk a Jira paginated list (`startAt`/`maxResults`/`total`/`isLast`)."""
    items: list[dict[str, Any]] = []
    start_at = 0
    while True:
        query: dict[str, Any] = {"startAt": start_at}
        if params:
            query.update(params)
        try:
            r = await client.get(path, params=query)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise_from_http_error(exc, tool_label=tool_label)
        except httpx.HTTPError as exc:
            raise_from_transport_error(exc, tool_label=tool_label)
        data: dict[str, Any] = r.json()
        page: list[dict[str, Any]] = data.get(items_key) or []
        items.extend(page)
        is_last = data.get("isLast")
        total = data.get("total")
        max_results = data.get("maxResults")
        if is_last is True or not page:
            break
        if total is not None:
            if len(items) >= int(total):
                break
        elif max_results is not None:
            if len(page) < int(max_results):
                break
        elif is_last is None:
            # No pagination metadata at all → treat as a single page rather
            # than loop forever on a server that returns a bare list.
            break
        start_at += len(page)
    return items


# -------------------- get_ticket --------------------


class GetTicketIn(BaseModel):
    key: str = Field(min_length=2, description="Jira issue key, e.g. ENG-12")


class JiraTicketOut(BaseModel):
    """Wrapper so tool callers don't depend on the signals model directly."""

    ticket: JiraTicket


_TICKET_FIELDS = "summary,status,assignee,reporter,created,updated,labels,issuelinks"


def _ticket_field_param() -> str:
    s = get_settings()
    parts = [_TICKET_FIELDS, s.jira_points_field]
    if s.jira_sprint_field:
        parts.append(s.jira_sprint_field)
    return ",".join(parts)


class GetTicketTool(BaseTool[GetTicketIn, JiraTicketOut]):
    name: ClassVar[str] = "get_ticket"
    description: ClassVar[str] = "Fetch one Jira ticket by key (assignee, status, points, etc.)."
    input_model: ClassVar[type[BaseModel]] = GetTicketIn
    output_model: ClassVar[type[BaseModel]] = JiraTicketOut

    async def _call(self, args: GetTicketIn) -> JiraTicketOut:
        async with _client() as client:
            try:
                r = await client.get(
                    f"{_api_base()}/issue/{args.key}", params={"fields": _ticket_field_param()}
                )
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_from_http_error(exc, tool_label=self.name)
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=self.name)
        issue: dict[str, Any] = r.json()
        return JiraTicketOut(ticket=_ticket_from_issue(issue))


# -------------------- get_ticket_history --------------------


class GetHistoryIn(BaseModel):
    key: str = Field(min_length=2)


class GetHistoryOut(BaseModel):
    key: str
    status_changes: list[JiraStatusChange] = Field(default_factory=list[JiraStatusChange])


class GetHistoryTool(BaseTool[GetHistoryIn, GetHistoryOut]):
    name: ClassVar[str] = "get_ticket_history"
    description: ClassVar[str] = (
        "Fetch the status-change history for one Jira ticket. "
        "Useful to see how long a ticket has been in its current status."
    )
    input_model: ClassVar[type[BaseModel]] = GetHistoryIn
    output_model: ClassVar[type[BaseModel]] = GetHistoryOut

    async def _call(self, args: GetHistoryIn) -> GetHistoryOut:
        async with _client() as client:
            values = await _paginate(
                client, f"{_api_base()}/issue/{args.key}/changelog", "values", self.name
            )
        return GetHistoryOut(key=args.key, status_changes=_status_changes(values))


# -------------------- get_ticket_dependencies --------------------


class GetDepsIn(BaseModel):
    key: str = Field(min_length=2)


class GetDepsOut(BaseModel):
    key: str
    blocks: list[str] = Field(default_factory=list[str])
    blocked_by: list[str] = Field(default_factory=list[str])


class GetDependenciesTool(BaseTool[GetDepsIn, GetDepsOut]):
    name: ClassVar[str] = "get_ticket_dependencies"
    description: ClassVar[str] = (
        "Return the upstream (blocked_by) and downstream (blocks) issue keys "
        "for a Jira ticket. Use this when judging whether a stuck ticket is "
        "holding up other work."
    )
    input_model: ClassVar[type[BaseModel]] = GetDepsIn
    output_model: ClassVar[type[BaseModel]] = GetDepsOut

    async def _call(self, args: GetDepsIn) -> GetDepsOut:
        async with _client() as client:
            try:
                r = await client.get(
                    f"{_api_base()}/issue/{args.key}", params={"fields": "issuelinks"}
                )
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_from_http_error(exc, tool_label=self.name)
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=self.name)
        payload: dict[str, Any] = r.json()
        fields: dict[str, Any] = payload.get("fields") or {}
        blocks, blocked_by = _links_from_fields(fields.get("issuelinks"))
        return GetDepsOut(key=args.key, blocks=list(blocks), blocked_by=list(blocked_by))


# -------------------- list_sprints (board discovery) --------------------


class ListSprintsIn(BaseModel):
    board_id: str = Field(min_length=1, description="Agile board id, e.g. ENG")
    state: str | None = Field(
        default=None,
        description="Optional Agile state filter, e.g. 'active' (else all sprints).",
    )


class ListSprintsOut(BaseModel):
    board_id: str
    sprints: list[JiraSprint] = Field(default_factory=list[JiraSprint])


class ListSprintsTool(BaseTool[ListSprintsIn, ListSprintsOut]):
    name: ClassVar[str] = "list_sprints"
    description: ClassVar[str] = (
        "List every sprint on an agile board with its name and state "
        "(active / closed / future). Use this to discover which sprint the "
        "team is currently working over before pulling its tickets."
    )
    input_model: ClassVar[type[BaseModel]] = ListSprintsIn
    output_model: ClassVar[type[BaseModel]] = ListSprintsOut

    async def _call(self, args: ListSprintsIn) -> ListSprintsOut:
        params = {"state": args.state} if args.state else None
        async with _client() as client:
            values = await _paginate(
                client,
                f"/rest/agile/1.0/board/{args.board_id}/sprint",
                "values",
                self.name,
                params=params,
            )
        return ListSprintsOut(
            board_id=args.board_id,
            sprints=[_sprint_from_meta(v, board_id=args.board_id) for v in values],
        )


# -------------------- list_sprint --------------------


class ListSprintIn(BaseModel):
    sprint_id: str | None = Field(default=None, description="Sprint id to pull tickets from.")
    board_id: str | None = Field(
        default=None,
        description="When sprint_id is omitted, the active sprint on this board is used.",
    )


class ListSprintOut(BaseModel):
    sprint_id: str
    start_date: datetime | None = None
    end_date: datetime | None = None
    tickets: list[JiraTicket] = Field(default_factory=list[JiraTicket])


class ListSprintTool(BaseTool[ListSprintIn, ListSprintOut]):
    name: ClassVar[str] = "list_sprint"
    description: ClassVar[str] = (
        "Return every ticket in a sprint (by id, or the active sprint on a "
        "board) along with the sprint's start/end dates. Callers derive the "
        "sprint day number and length from those dates."
    )
    input_model: ClassVar[type[BaseModel]] = ListSprintIn
    output_model: ClassVar[type[BaseModel]] = ListSprintOut

    async def _call(self, args: ListSprintIn) -> ListSprintOut:
        async with _client() as client:
            meta = await self._resolve_sprint(client, args)
            sprint_id = str(meta["id"])
            issues = await _paginate(
                client, f"/rest/agile/1.0/sprint/{sprint_id}/issue", "issues", self.name
            )
        return ListSprintOut(
            sprint_id=sprint_id,
            start_date=_dt(meta["startDate"]) if meta.get("startDate") else None,
            end_date=_dt(meta["endDate"]) if meta.get("endDate") else None,
            tickets=[_ticket_from_issue(i) for i in issues],
        )

    async def _resolve_sprint(
        self, client: httpx.AsyncClient, args: ListSprintIn
    ) -> dict[str, Any]:
        """Resolve the sprint metadata dict (with id + start/end dates)."""
        if args.sprint_id:
            try:
                r = await client.get(f"/rest/agile/1.0/sprint/{args.sprint_id}")
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_from_http_error(exc, tool_label=self.name)
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=self.name)
            meta: dict[str, Any] = r.json()
            return meta
        if args.board_id:
            try:
                r = await client.get(
                    f"/rest/agile/1.0/board/{args.board_id}/sprint", params={"state": "active"}
                )
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_from_http_error(exc, tool_label=self.name)
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=self.name)
            values: list[dict[str, Any]] = r.json().get("values") or []
            if not values:
                raise ToolException(
                    kind=ToolErrorKind.NOT_FOUND,
                    message=f"{self.name}: no active sprint on board {args.board_id}",
                )
            return values[0]
        raise ToolException(
            kind=ToolErrorKind.VALIDATION,
            message=f"{self.name}: one of sprint_id or board_id is required",
        )


# -------------------- post_jira_comment (WRITE) --------------------


class PostCommentIn(BaseModel):
    key: str = Field(min_length=2)
    body: str = Field(min_length=1, max_length=2000)


class PostCommentOut(BaseModel):
    key: str
    comment_id: str
    posted_at: datetime


class PostCommentTool(BaseTool[PostCommentIn, PostCommentOut]):
    name: ClassVar[str] = "post_jira_comment"
    description: ClassVar[str] = (
        "Post a comment on a Jira ticket. Side-effecting; only invoked after Phase 8 TL approval."
    )
    input_model: ClassVar[type[BaseModel]] = PostCommentIn
    output_model: ClassVar[type[BaseModel]] = PostCommentOut
    requires_approval: ClassVar[bool] = True
    retry_policy: ClassVar[RetryPolicy] = RetryPolicy(max_attempts=2, initial_delay_ms=200)

    def idempotency_key(self, args: PostCommentIn, *, run_date_iso: str) -> str | None:
        return make_key(tool_name=self.name, args=args, run_date_iso=run_date_iso)

    async def _call(self, args: PostCommentIn) -> PostCommentOut:
        async with _client() as client:
            try:
                r = await client.post(
                    f"{_api_base()}/issue/{args.key}/comment",
                    json=_comment_payload(args.body),
                )
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_from_http_error(exc, tool_label=self.name)
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=self.name)
        data: dict[str, Any] = r.json()
        # Jira returns the created comment object: {id, author, body, created, …}.
        created = data.get("created") or data.get("posted_at")
        return PostCommentOut(
            key=args.key,
            comment_id=str(data.get("id") or data.get("comment_id") or ""),
            posted_at=_dt(created) if created else datetime.now(UTC),
        )


# -------------------- registration --------------------


def register_jira_tools() -> None:
    """Add the Jira tools to the module-level registry. Idempotent on re-call."""
    import contextlib

    from tl_agent.tools.registry import RegistryError

    for tool_cls in (
        GetTicketTool,
        GetHistoryTool,
        GetDependenciesTool,
        ListSprintsTool,
        ListSprintTool,
        PostCommentTool,
    ):
        # Idempotent: collision means it's already registered, that's fine.
        with contextlib.suppress(RegistryError):
            registry.register(tool_cls())
