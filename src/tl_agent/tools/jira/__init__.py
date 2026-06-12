"""Jira tools.

Calls the real Jira REST API. In compose we point at the in-process
`services/jira_mock` (a FastAPI subset that now emits *real-shaped* Jira
JSON); the same code talks to a live Jira given the right `jira_base_url` +
token in settings.

Submodules:
- `_shared` — HTTP client, API-version helpers, pagination
- `_mappers` — envelope → domain model translation
- `tickets` — get_ticket, get_ticket_history, get_ticket_dependencies, post_jira_comment
- `boards` — list_boards, list_sprints, list_sprint

All inputs/outputs are Pydantic models; HTTP errors → typed ToolExceptions.
"""

from __future__ import annotations

import contextlib

from tl_agent.tools.jira.boards import (
    JiraBoard,
    ListBoardsIn,
    ListBoardsOut,
    ListBoardsTool,
    ListSprintIn,
    ListSprintOut,
    ListSprintsIn,
    ListSprintsOut,
    ListSprintsTool,
    ListSprintTool,
)
from tl_agent.tools.jira.tickets import (
    GetDependenciesTool,
    GetDepsIn,
    GetDepsOut,
    GetHistoryIn,
    GetHistoryOut,
    GetHistoryTool,
    GetTicketIn,
    GetTicketTool,
    JiraTicketOut,
    PostCommentIn,
    PostCommentOut,
    PostCommentTool,
)
from tl_agent.tools.registry import RegistryError, registry

__all__ = [
    "GetDependenciesTool",
    "GetDepsIn",
    "GetDepsOut",
    "GetHistoryIn",
    "GetHistoryOut",
    "GetHistoryTool",
    "GetTicketIn",
    "GetTicketTool",
    "JiraBoard",
    "JiraTicketOut",
    "ListBoardsIn",
    "ListBoardsOut",
    "ListBoardsTool",
    "ListSprintIn",
    "ListSprintOut",
    "ListSprintTool",
    "ListSprintsIn",
    "ListSprintsOut",
    "ListSprintsTool",
    "PostCommentIn",
    "PostCommentOut",
    "PostCommentTool",
    "register_jira_tools",
]


def register_jira_tools() -> None:
    """Add the Jira tools to the module-level registry. Idempotent on re-call."""
    for tool_cls in (
        GetTicketTool,
        GetHistoryTool,
        GetDependenciesTool,
        ListBoardsTool,
        ListSprintsTool,
        ListSprintTool,
        PostCommentTool,
    ):
        # Idempotent: collision means it's already registered, that's fine.
        with contextlib.suppress(RegistryError):
            registry.register(tool_cls())
