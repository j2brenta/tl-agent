"""Jira tools.

Calls a Jira-shaped REST API. In compose we point at the in-process
`services/jira_mock` (FastAPI subset); the same code talks to the real Jira
cloud API given the right `jira_base_url` + token in settings.

Tools registered here:
- get_ticket (read)
- get_ticket_history (read)
- get_ticket_dependencies (read)
- list_sprint (read)
- post_jira_comment (WRITE — requires_approval=True; Phase 5 excluded)

All inputs/outputs are Pydantic models; HTTP errors → typed ToolExceptions.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

import httpx
from pydantic import BaseModel, Field

from tl_agent.models import JiraSprint, JiraStatusChange, JiraTicket
from tl_agent.settings import get_settings
from tl_agent.tools._http import http_client, raise_from_http_error, raise_from_transport_error
from tl_agent.tools.base import BaseTool, RetryPolicy
from tl_agent.tools.idempotency import make_key
from tl_agent.tools.registry import registry


def _client() -> httpx.AsyncClient:
    s = get_settings()
    return http_client(
        base_url=s.jira_base_url,
        headers={"Authorization": f"Bearer {s.jira_token}", "Accept": "application/json"},
    )


# -------------------- get_ticket --------------------


class GetTicketIn(BaseModel):
    key: str = Field(min_length=2, description="Jira issue key, e.g. ENG-12")


class JiraTicketOut(BaseModel):
    """Wrapper so tool callers don't depend on the signals model directly."""

    ticket: JiraTicket


class GetTicketTool(BaseTool[GetTicketIn, JiraTicketOut]):
    name: ClassVar[str] = "get_ticket"
    description: ClassVar[str] = "Fetch one Jira ticket by key (assignee, status, points, etc.)."
    input_model: ClassVar[type[BaseModel]] = GetTicketIn
    output_model: ClassVar[type[BaseModel]] = JiraTicketOut

    async def _call(self, args: GetTicketIn) -> JiraTicketOut:
        async with _client() as client:
            try:
                r = await client.get(f"/rest/api/3/issue/{args.key}")
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_from_http_error(exc, tool_label=self.name)
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=self.name)
        return JiraTicketOut(ticket=JiraTicket.model_validate(r.json()))


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
            try:
                r = await client.get(f"/rest/api/3/issue/{args.key}/changelog")
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_from_http_error(exc, tool_label=self.name)
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=self.name)
        data = r.json()
        changes = [JiraStatusChange.model_validate(item) for item in data.get("values", [])]
        return GetHistoryOut(key=args.key, status_changes=changes)


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
                r = await client.get(f"/rest/api/3/issue/{args.key}/links")
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_from_http_error(exc, tool_label=self.name)
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=self.name)
        data = r.json()
        return GetDepsOut(
            key=args.key,
            blocks=list(data.get("blocks", [])),
            blocked_by=list(data.get("blocked_by", [])),
        )


# -------------------- list_sprints (board discovery) --------------------


class ListSprintsIn(BaseModel):
    board_id: str = Field(min_length=1, description="Agile board id, e.g. ENG")


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
        async with _client() as client:
            try:
                r = await client.get(f"/rest/agile/1.0/board/{args.board_id}/sprint")
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_from_http_error(exc, tool_label=self.name)
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=self.name)
        data = r.json()
        return ListSprintsOut(
            board_id=args.board_id,
            sprints=[JiraSprint.model_validate(s) for s in data.get("values", [])],
        )


# -------------------- list_sprint --------------------


class ListSprintIn(BaseModel):
    sprint_id: str | None = Field(
        default=None, description="Sprint identifier; defaults to the active sprint."
    )


class ListSprintOut(BaseModel):
    sprint_id: str
    sprint_day: int = Field(ge=1)
    sprint_length_days: int = Field(ge=1)
    tickets: list[JiraTicket] = Field(default_factory=list[JiraTicket])
    added_since: datetime | None = None  # timestamp of "added since this"


class ListSprintTool(BaseTool[ListSprintIn, ListSprintOut]):
    name: ClassVar[str] = "list_sprint"
    description: ClassVar[str] = (
        "Return every ticket in the active (or specified) sprint along with "
        "the sprint day number, length, and the cut-off timestamp for the "
        "'added since' delta. The agent uses this to find mid-sprint additions."
    )
    input_model: ClassVar[type[BaseModel]] = ListSprintIn
    output_model: ClassVar[type[BaseModel]] = ListSprintOut

    async def _call(self, args: ListSprintIn) -> ListSprintOut:
        path = (
            f"/rest/agile/1.0/sprint/{args.sprint_id}/issue"
            if args.sprint_id
            else "/rest/agile/1.0/sprint/active/issue"
        )
        async with _client() as client:
            try:
                r = await client.get(path)
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_from_http_error(exc, tool_label=self.name)
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=self.name)
        data = r.json()
        return ListSprintOut(
            sprint_id=data["sprint_id"],
            sprint_day=int(data["sprint_day"]),
            sprint_length_days=int(data["sprint_length_days"]),
            tickets=[JiraTicket.model_validate(t) for t in data.get("issues", [])],
            added_since=datetime.fromisoformat(data["added_since"])
            if data.get("added_since")
            else None,
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
                    f"/rest/api/3/issue/{args.key}/comment",
                    json={"body": args.body},
                )
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_from_http_error(exc, tool_label=self.name)
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=self.name)
        return PostCommentOut.model_validate(r.json())


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
