"""Single-ticket Jira tools: read a ticket, its history, its dependencies, and
post a comment back to it.

Tools registered here:
- get_ticket (read)
- get_ticket_history (read)
- get_ticket_dependencies (read)
- post_jira_comment (WRITE — requires_approval=True; Phase 5 excluded)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, Field

from tl_agent.models import JiraStatusChange, JiraTicket
from tl_agent.settings import get_settings
from tl_agent.tools._http import raise_from_http_error, raise_from_transport_error
from tl_agent.tools.base import BaseTool, RetryPolicy
from tl_agent.tools.idempotency import make_key
from tl_agent.tools.jira._mappers import (
    links_from_fields,
    parse_dt,
    status_changes,
    ticket_from_issue,
)
from tl_agent.tools.jira._shared import api_base, comment_payload, jira_client, paginate

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
        async with jira_client() as client:
            try:
                r = await client.get(
                    f"{api_base()}/issue/{args.key}", params={"fields": _ticket_field_param()}
                )
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_from_http_error(exc, tool_label=self.name)
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=self.name)
        issue: dict[str, Any] = r.json()
        return JiraTicketOut(ticket=ticket_from_issue(issue))


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
        async with jira_client() as client:
            values = await paginate(
                client, f"{api_base()}/issue/{args.key}/changelog", "values", self.name
            )
        return GetHistoryOut(key=args.key, status_changes=status_changes(values))


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
        async with jira_client() as client:
            try:
                r = await client.get(
                    f"{api_base()}/issue/{args.key}", params={"fields": "issuelinks"}
                )
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_from_http_error(exc, tool_label=self.name)
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=self.name)
        payload: dict[str, Any] = r.json()
        fields: dict[str, Any] = payload.get("fields") or {}
        blocks, blocked_by = links_from_fields(fields.get("issuelinks"))
        return GetDepsOut(key=args.key, blocks=list(blocks), blocked_by=list(blocked_by))


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
        async with jira_client() as client:
            try:
                r = await client.post(
                    f"{api_base()}/issue/{args.key}/comment",
                    json=comment_payload(args.body),
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
            posted_at=parse_dt(created) if created else datetime.now(UTC),
        )
