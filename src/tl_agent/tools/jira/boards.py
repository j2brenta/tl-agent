"""Board and sprint discovery, and sprint ticket listing — the Agile API surface.

Tools registered here:
- list_boards (read) — board discovery
- list_sprints (read) — board sprint discovery
- list_sprint (read) — tickets in a sprint
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, Field

from tl_agent.models import JiraSprint, JiraTicket
from tl_agent.tools._http import raise_from_http_error, raise_from_transport_error
from tl_agent.tools.base import BaseTool, ToolErrorKind, ToolException
from tl_agent.tools.jira._mappers import parse_dt, sprint_from_meta, ticket_from_issue
from tl_agent.tools.jira._shared import jira_client, paginate

logger = logging.getLogger(__name__)


def _board_from_meta(board: dict[str, Any]) -> JiraBoard:
    location: dict[str, Any] = board.get("location") or {}
    return JiraBoard(
        id=str(board["id"]),
        name=board.get("name") or str(board["id"]),
        type=board.get("type"),
        project_key=location.get("projectKey"),
    )


# -------------------- list_boards (board discovery) --------------------


class ListBoardsIn(BaseModel):
    project: str | None = Field(
        default=None, description="Optional Jira projectKeyOrId to scope the board list."
    )


class JiraBoard(BaseModel):
    id: str = Field(min_length=1)
    name: str
    type: str | None = None
    project_key: str | None = None


class ListBoardsOut(BaseModel):
    boards: list[JiraBoard] = Field(default_factory=list[JiraBoard])


class ListBoardsTool(BaseTool[ListBoardsIn, ListBoardsOut]):
    name: ClassVar[str] = "list_boards"
    description: ClassVar[str] = (
        "List the Jira agile boards (optionally scoped to a project). Used to "
        "discover the team's board when `board_id` isn't configured."
    )
    input_model: ClassVar[type[BaseModel]] = ListBoardsIn
    output_model: ClassVar[type[BaseModel]] = ListBoardsOut

    async def _call(self, args: ListBoardsIn) -> ListBoardsOut:
        params = {"projectKeyOrId": args.project} if args.project else None
        async with jira_client() as client:
            values = await paginate(
                client, "/rest/agile/1.0/board", "values", self.name, params=params
            )
        return ListBoardsOut(boards=[_board_from_meta(v) for v in values])


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
        async with jira_client() as client:
            values = await paginate(
                client,
                f"/rest/agile/1.0/board/{args.board_id}/sprint",
                "values",
                self.name,
                params=params,
            )
        return ListSprintsOut(
            board_id=args.board_id,
            sprints=[sprint_from_meta(v, board_id=args.board_id) for v in values],
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
        async with jira_client() as client:
            meta = await self._resolve_sprint(client, args)
            sprint_id = str(meta["id"])
            endpoint = f"/rest/agile/1.0/sprint/{sprint_id}/issue"
            issues = await paginate(client, endpoint, "issues", self.name)
        logger.info(
            "list_sprint.tickets",
            extra={
                "sprint_id": sprint_id,
                "sprint_name": meta.get("name"),
                "endpoint": endpoint,
                "tickets": len(issues),
            },
        )
        return ListSprintOut(
            sprint_id=sprint_id,
            start_date=parse_dt(meta["startDate"]) if meta.get("startDate") else None,
            end_date=parse_dt(meta["endDate"]) if meta.get("endDate") else None,
            tickets=[ticket_from_issue(i) for i in issues],
        )

    async def _resolve_sprint(
        self, client: httpx.AsyncClient, args: ListSprintIn
    ) -> dict[str, Any]:
        """Resolve the sprint metadata dict (with id + start/end dates).

        Logs which Agile endpoint was queried and what came back so a
        NOT_FOUND is diagnosable from the trace: by `sprint_id` it's a direct
        GET; by `board_id` it lists the board's *active* sprints and takes the
        first. An empty active list is the usual "list_sprint not found" cause.
        """
        if args.sprint_id:
            return await self._resolve_by_sprint_id(client, args.sprint_id)
        if args.board_id:
            return await self._resolve_by_board_id(client, args.board_id)
        logger.warning(
            "list_sprint.not_found", extra={"by": "none", "reason": "no sprint_id or board_id"}
        )
        raise ToolException(
            kind=ToolErrorKind.VALIDATION,
            message=f"{self.name}: one of sprint_id or board_id is required",
        )

    async def _resolve_by_sprint_id(
        self, client: httpx.AsyncClient, sprint_id: str
    ) -> dict[str, Any]:
        endpoint = f"/rest/agile/1.0/sprint/{sprint_id}"
        try:
            r = await client.get(endpoint)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "list_sprint.resolve_failed",
                extra={
                    "by": "sprint_id",
                    "sprint_id": sprint_id,
                    "endpoint": endpoint,
                    "status": exc.response.status_code,
                },
            )
            raise_from_http_error(exc, tool_label=self.name)
        except httpx.HTTPError as exc:
            logger.warning(
                "list_sprint.resolve_failed",
                extra={"by": "sprint_id", "sprint_id": sprint_id, "endpoint": endpoint},
            )
            raise_from_transport_error(exc, tool_label=self.name)
        meta: dict[str, Any] = r.json()
        logger.info(
            "list_sprint.resolved",
            extra={
                "by": "sprint_id",
                "endpoint": endpoint,
                "sprint_id": str(meta.get("id")),
                "sprint_name": meta.get("name"),
                "state": meta.get("state"),
            },
        )
        return meta

    async def _resolve_by_board_id(
        self, client: httpx.AsyncClient, board_id: str
    ) -> dict[str, Any]:
        endpoint = f"/rest/agile/1.0/board/{board_id}/sprint"
        try:
            r = await client.get(endpoint, params={"state": "active"})
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "list_sprint.resolve_failed",
                extra={
                    "by": "board_id",
                    "board_id": board_id,
                    "endpoint": endpoint,
                    "state": "active",
                    "status": exc.response.status_code,
                },
            )
            raise_from_http_error(exc, tool_label=self.name)
        except httpx.HTTPError as exc:
            logger.warning(
                "list_sprint.resolve_failed",
                extra={
                    "by": "board_id",
                    "board_id": board_id,
                    "endpoint": endpoint,
                    "state": "active",
                },
            )
            raise_from_transport_error(exc, tool_label=self.name)
        values: list[dict[str, Any]] = r.json().get("values") or []
        if not values:
            logger.warning(
                "list_sprint.not_found",
                extra={
                    "by": "board_id",
                    "board_id": board_id,
                    "endpoint": endpoint,
                    "state": "active",
                    "active_sprints": 0,
                },
            )
            raise ToolException(
                kind=ToolErrorKind.NOT_FOUND,
                message=(
                    f"{self.name}: no active sprint on board {board_id} "
                    f"(GET {endpoint}?state=active returned 0 sprints) — start a "
                    f"sprint on the board or pass an explicit sprint_id"
                ),
            )
        chosen = values[0]
        logger.info(
            "list_sprint.resolved",
            extra={
                "by": "board_id",
                "board_id": board_id,
                "endpoint": endpoint,
                "active_sprints": len(values),
                "sprint_id": str(chosen.get("id")),
                "sprint_name": chosen.get("name"),
            },
        )
        return chosen
