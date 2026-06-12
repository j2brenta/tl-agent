"""Tests for the concrete Jira/GitLab/memory tools.

We use pytest-httpx to stub the HTTP layer — no real services touched.
End-to-end against the compose stack lives in tests/integration/.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, date, datetime

import pytest
from pytest_httpx import HTTPXMock

from tl_agent.storage import connect, initialize, transaction
from tl_agent.storage.repos import baselines, observations
from tl_agent.storage.repos.baselines import Baseline
from tl_agent.tools import ToolErrorKind, ToolResult, registry
from tl_agent.tools.gitlab import (
    GetCommitDiffTool,
    ListBranchesTool,
    ListCommitsTool,
    ListGroupProjectsTool,
    _parse_ticket_keys,
    register_gitlab_tools,
)
from tl_agent.tools.jira import (
    GetDependenciesTool,
    GetHistoryTool,
    GetTicketTool,
    ListBoardsTool,
    ListSprintsTool,
    ListSprintTool,
    PostCommentTool,
    register_jira_tools,
)
from tl_agent.tools.memory import (
    GetBaselineTool,
    SearchStandupHistoryTool,
    register_memory_tools,
    set_sqlite_connection,
)


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    registry.reset()


# -------------------- registration --------------------


def test_register_all_tools_no_collisions() -> None:
    register_jira_tools()
    register_gitlab_tools()
    register_memory_tools()
    names = sorted(t.name for t in registry.all())
    assert "get_ticket" in names
    assert "list_commits" in names
    assert "list_group_projects" in names
    assert "search_standup_history" in names
    assert "post_jira_comment" in names
    assert "list_sprints" in names

    # Phase-5 binding excludes writers
    phase5_names = {t.name for t in registry.for_phase5()}
    assert "post_jira_comment" not in phase5_names
    assert "get_ticket" in phase5_names


# -------------------- jira --------------------


_BLOCKS_LINK = {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"}


def _issue(key: str, **fields: object) -> dict[str, object]:
    """Build a real-shaped Jira issue envelope with sensible defaults."""
    base: dict[str, object] = {
        "summary": "add retry",
        "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        "assignee": {"displayName": "john"},
        "reporter": {"displayName": "tl"},
        "created": "2026-05-20T09:00:00+00:00",
        "updated": "2026-05-22T09:00:00+00:00",
        "labels": [],
        "customfield_10016": 5,
        "issuelinks": [],
    }
    base.update(fields)
    return {"key": key, "fields": base}


async def test_get_ticket_happy_path(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"http://localhost:9100/rest/api/3/issue/ENG-12"),
        json=_issue(
            "ENG-12",
            labels=["billing"],
            issuelinks=[
                {"type": _BLOCKS_LINK, "outwardIssue": {"key": "ENG-19"}},
                {"type": _BLOCKS_LINK, "inwardIssue": {"key": "ENG-9"}},
            ],
        ),
    )
    tool = GetTicketTool()
    result = await tool.invoke({"key": "ENG-12"}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolResult)
    t = result.value.ticket
    assert t.key == "ENG-12"
    assert t.assignee == "john"
    assert t.status.value == "in_progress"
    assert t.points == 5.0
    assert t.blocks == ("ENG-19",)
    assert t.blocked_by == ("ENG-9",)


async def test_get_ticket_assignee_falls_back_to_account_id(httpx_mock: HTTPXMock) -> None:
    # Cloud privacy can hide displayName/name → accountId is the only identity.
    httpx_mock.add_response(
        url=re.compile(r".*/issue/ENG-12"),
        json=_issue("ENG-12", assignee={"accountId": "5b10ac8d82e05b22cc7d4ef5"}),
    )
    result = await GetTicketTool().invoke({"key": "ENG-12"}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolResult)
    assert result.value.ticket.assignee == "5b10ac8d82e05b22cc7d4ef5"


async def test_get_ticket_uses_v2_prefix(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TLA_JIRA_API_VERSION", "2")
    httpx_mock.add_response(
        url=re.compile(r"http://localhost:9100/rest/api/2/issue/ENG-12"),
        json=_issue("ENG-12"),
    )
    result = await GetTicketTool().invoke({"key": "ENG-12"}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolResult)
    assert result.value.ticket.key == "ENG-12"
    assert httpx_mock.get_requests()[0].url.path == "/rest/api/2/issue/ENG-12"


async def test_get_ticket_status_falls_back_to_category(httpx_mock: HTTPXMock) -> None:
    # A status name the map doesn't know → bucket via statusCategory.
    httpx_mock.add_response(
        url=re.compile(r".*/issue/ENG-12"),
        json=_issue("ENG-12", status={"name": "Awaiting QA", "statusCategory": {"key": "done"}}),
    )
    result = await GetTicketTool().invoke({"key": "ENG-12"}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolResult)
    assert result.value.ticket.status.value == "done"


async def test_get_ticket_404_returns_typed_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*/issue/MISSING-1"),
        status_code=404,
        json={"error": "not found"},
    )
    tool = GetTicketTool()
    result = await tool.invoke({"key": "MISSING-1"}, run_date_iso="2026-05-22")
    assert hasattr(result, "kind")
    assert result.kind is ToolErrorKind.NOT_FOUND  # type: ignore[union-attr]


async def test_get_ticket_history(httpx_mock: HTTPXMock) -> None:
    # Real changelog: status change is one item among others — only it counts.
    httpx_mock.add_response(
        url=re.compile(r".*/issue/ENG-12/changelog"),
        json={
            "values": [
                {
                    "created": "2026-05-20T10:00:00+00:00",
                    "author": {"displayName": "john"},
                    "items": [
                        {"field": "assignee", "fromString": "x", "toString": "y"},
                        {"field": "status", "fromString": "To Do", "toString": "In Progress"},
                    ],
                }
            ],
            "isLast": True,
            "total": 1,
            "startAt": 0,
            "maxResults": 50,
        },
    )
    result = await GetHistoryTool().invoke({"key": "ENG-12"}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolResult)
    assert len(result.value.status_changes) == 1
    assert result.value.status_changes[0].from_status.value == "todo"
    assert result.value.status_changes[0].to_status.value == "in_progress"


async def test_get_dependencies_from_issuelinks(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*/issue/ENG-12"),
        json=_issue(
            "ENG-12",
            issuelinks=[
                {"type": _BLOCKS_LINK, "outwardIssue": {"key": "ENG-19"}},
                {"type": _BLOCKS_LINK, "inwardIssue": {"key": "ENG-9"}},
                # A non-blocking link must be ignored.
                {
                    "type": {"name": "Relates", "outward": "relates to"},
                    "outwardIssue": {"key": "ENG-3"},
                },
            ],
        ),
    )
    result = await GetDependenciesTool().invoke({"key": "ENG-12"}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolResult)
    assert result.value.blocks == ["ENG-19"]
    assert result.value.blocked_by == ["ENG-9"]


async def test_list_sprint_by_id(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*/rest/agile/1.0/sprint/S-2026-05$"),
        json={
            "id": "S-2026-05",
            "name": "Eng Sprint 19",
            "state": "active",
            "startDate": "2026-05-19T09:00:00+00:00",
            "endDate": "2026-05-29T09:00:00+00:00",
        },
    )
    httpx_mock.add_response(
        url=re.compile(r".*/rest/agile/1.0/sprint/S-2026-05/issue"),
        json={
            "issues": [_issue("ENG-1", summary="rate limiter")],
            "isLast": True,
            "total": 1,
            "startAt": 0,
            "maxResults": 50,
        },
    )
    result = await ListSprintTool().invoke({"sprint_id": "S-2026-05"}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolResult)
    assert result.value.sprint_id == "S-2026-05"
    assert result.value.start_date is not None
    assert [t.key for t in result.value.tickets] == ["ENG-1"]


async def test_list_sprint_by_board_discovers_active(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*/board/ENG/sprint"),
        json={
            "values": [
                {
                    "id": "S-2026-05",
                    "name": "Eng Sprint 19",
                    "state": "active",
                    "startDate": "2026-05-19T09:00:00+00:00",
                    "endDate": "2026-05-29T09:00:00+00:00",
                }
            ],
            "isLast": True,
        },
    )
    httpx_mock.add_response(
        url=re.compile(r".*/sprint/S-2026-05/issue"),
        json={"issues": [], "isLast": True, "total": 0},
    )
    result = await ListSprintTool().invoke({"board_id": "ENG"}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolResult)
    assert result.value.sprint_id == "S-2026-05"


async def test_list_boards(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*/rest/agile/1.0/board(\?|$)"),
        json={
            "values": [
                {
                    "id": 11,
                    "name": "Engineering",
                    "type": "scrum",
                    "location": {"projectKey": "ENG"},
                },
                {"id": 22, "name": "Ops", "type": "kanban"},
            ],
            "isLast": True,
        },
    )
    result = await ListBoardsTool().invoke({}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolResult)
    assert [(b.id, b.project_key) for b in result.value.boards] == [("11", "ENG"), ("22", None)]


async def test_list_sprints(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*/board/ENG/sprint"),
        json={
            "values": [
                {"id": "S-2026-04", "name": "Eng Sprint 18", "state": "closed"},
                {
                    "id": "S-2026-05",
                    "name": "Eng Sprint 19",
                    "state": "active",
                    "originBoardId": "ENG",
                    "startDate": "2026-05-19T09:00:00+00:00",
                    "endDate": "2026-05-29T09:00:00+00:00",
                },
            ],
            "isLast": True,
        },
    )
    result = await ListSprintsTool().invoke({"board_id": "ENG"}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolResult)
    assert [s.id for s in result.value.sprints] == ["S-2026-04", "S-2026-05"]
    active = [s for s in result.value.sprints if s.state.value == "active"]
    assert active[0].name == "Eng Sprint 19"
    assert active[0].start_date is not None


async def test_post_jira_comment_v3_posts_adf(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=re.compile(r"http://localhost:9100/rest/api/3/issue/ENG-12/comment"),
        method="POST",
        json={"id": "C-1", "created": "2026-05-22T10:00:00+00:00", "author": {"displayName": "tl"}},
    )
    result = await PostCommentTool().invoke(
        {"key": "ENG-12", "body": "hi"}, run_date_iso="2026-05-22"
    )
    assert isinstance(result, ToolResult)
    assert result.value.comment_id == "C-1"
    sent = json.loads(httpx_mock.get_requests()[-1].content)
    assert sent["body"]["type"] == "doc"
    assert sent["body"]["content"][0]["content"][0]["text"] == "hi"


async def test_post_jira_comment_v2_posts_plain(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TLA_JIRA_API_VERSION", "2")
    httpx_mock.add_response(
        url=re.compile(r"http://localhost:9100/rest/api/2/issue/ENG-12/comment"),
        method="POST",
        json={"id": "C-2", "created": "2026-05-22T10:00:00+00:00"},
    )
    result = await PostCommentTool().invoke(
        {"key": "ENG-12", "body": "hi"}, run_date_iso="2026-05-22"
    )
    assert isinstance(result, ToolResult)
    assert result.value.comment_id == "C-2"
    sent = json.loads(httpx_mock.get_requests()[-1].content)
    assert sent["body"] == "hi"


# -------------------- gitlab --------------------


def test_parse_ticket_keys() -> None:
    msg = "ENG-12 add retry; also touches PAY-3 path"
    assert _parse_ticket_keys(msg) == ("ENG-12", "PAY-3")


async def test_list_commits(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=(
            "http://localhost:8929/api/v4/projects/tl-agent%2Fdemo/repository/commits"
            "?since=2026-05-21T12:00:00%2B00:00&until=2026-05-22T12:00:00%2B00:00"
            "&with_stats=true&all=true"
        ),
        json=[
            {
                "id": "abc1234def5678",
                "author_email": "john@example.local",
                "committed_date": "2026-05-22T09:00:00Z",
                "message": "ENG-12 wip retry",
                "stats": {"files": 3, "additions": 42, "deletions": 10},
            }
        ],
    )
    result = await ListCommitsTool().invoke(
        {
            "project": "tl-agent/demo",
            "since": "2026-05-21T12:00:00+00:00",
            "until": "2026-05-22T12:00:00+00:00",
        },
        run_date_iso="2026-05-22",
    )
    assert isinstance(result, ToolResult)
    assert len(result.value.commits) == 1
    c = result.value.commits[0]
    assert c.linked_ticket_keys == ("ENG-12",)
    assert c.insertions == 42
    assert c.project == "tl-agent/demo"


async def test_get_commit_diff(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://localhost:8929/api/v4/projects/tl-agent%2Fdemo/repository/commits/abc1234/diff",
        json=[
            {"new_path": "a.py", "additions": 10, "deletions": 1, "diff": "+ ..."},
            {"new_path": "b.py", "additions": 32, "deletions": 9, "diff": "+ ..."},
        ],
    )
    result = await GetCommitDiffTool().invoke(
        {"project": "tl-agent/demo", "sha": "abc1234"}, run_date_iso="2026-05-22"
    )
    assert isinstance(result, ToolResult)
    assert result.value.total_additions == 42
    assert result.value.total_deletions == 10


async def test_list_branches(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://localhost:8929/api/v4/projects/tl-agent%2Fdemo/repository/branches",
        json=[
            {
                "name": "main",
                "commit": {
                    "id": "deadbeef00",
                    "committed_date": "2026-05-22T08:00:00Z",
                    "author_email": "john@example.local",
                },
            }
        ],
    )
    result = await ListBranchesTool().invoke(
        {"project": "tl-agent/demo"}, run_date_iso="2026-05-22"
    )
    assert isinstance(result, ToolResult)
    assert result.value.branches[0].name == "main"


async def test_list_group_projects(httpx_mock: HTTPXMock) -> None:
    """Paginates `page`/`per_page` until a short page ends it."""
    full_page = [{"path_with_namespace": f"tl-agent/repo{i}"} for i in range(100)]
    httpx_mock.add_response(
        url="http://localhost:8929/api/v4/groups/tl-agent/projects"
        "?include_subgroups=true&simple=true&per_page=100&page=1",
        json=full_page,
    )
    httpx_mock.add_response(
        url="http://localhost:8929/api/v4/groups/tl-agent/projects"
        "?include_subgroups=true&simple=true&per_page=100&page=2",
        json=[{"path_with_namespace": "tl-agent/demo"}],
    )
    result = await ListGroupProjectsTool().invoke({"group": "tl-agent"}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolResult)
    assert len(result.value.projects) == 101
    assert "tl-agent/demo" in result.value.projects


# -------------------- memory (sqlite-backed) --------------------


@pytest.fixture
def memory_conn() -> sqlite3.Connection:
    conn = connect(":memory:")
    initialize(conn)
    # Seed some standups + a baseline.
    with transaction(conn):
        observations.upsert(
            conn,
            obs_id="o1",
            run_date=date(2026, 5, 22),
            engineer_id="john",
            raw="still working on the publisher retry",
            summary="publisher",
            chat_message_id=None,
        )
        baselines.upsert(
            conn,
            Baseline(
                engineer_id="john",
                window="7d",
                metric="standup_line_count_avg",
                value=4.0,
                computed_at=datetime(2026, 5, 22, 9, 0, tzinfo=UTC),
            ),
        )
    set_sqlite_connection(lambda: conn)
    return conn


async def test_search_standup_history_uses_fts(memory_conn: sqlite3.Connection) -> None:
    del memory_conn
    result = await SearchStandupHistoryTool().invoke(
        {"query": "publisher", "engineer_id": "john"}, run_date_iso="2026-05-22"
    )
    assert isinstance(result, ToolResult)
    assert result.value.hits
    assert result.value.hits[0].engineer_id == "john"


async def test_get_baseline_returns_none_when_missing(memory_conn: sqlite3.Connection) -> None:
    del memory_conn
    result = await GetBaselineTool().invoke(
        {"engineer_id": "matt", "window": "7d", "metric": "standup_line_count_avg"},
        run_date_iso="2026-05-22",
    )
    assert isinstance(result, ToolResult)
    assert result.value.baseline is None


async def test_get_baseline_returns_value_when_present(memory_conn: sqlite3.Connection) -> None:
    del memory_conn
    result = await GetBaselineTool().invoke(
        {"engineer_id": "john", "window": "7d", "metric": "standup_line_count_avg"},
        run_date_iso="2026-05-22",
    )
    assert isinstance(result, ToolResult)
    assert result.value.baseline is not None
    assert result.value.baseline.value == 4.0


# -------------------- gitlab project allowlist --------------------


async def test_list_commits_rejects_unknown_project() -> None:
    """LLM-invented project paths fail VALIDATION before any HTTP call."""
    result = await ListCommitsTool().invoke(
        {
            "project": "acme/backend",
            "since": "2026-05-21T12:00:00+00:00",
            "until": "2026-05-22T12:00:00+00:00",
        },
        run_date_iso="2026-05-22",
    )
    assert isinstance(result, type(result))  # always true; assert below is the real check
    assert result.kind is ToolErrorKind.VALIDATION  # type: ignore[union-attr]
    assert "acme/backend" in result.message  # type: ignore[union-attr]
    assert "tl-agent" in result.message  # type: ignore[union-attr]


async def test_get_commit_diff_rejects_unknown_project() -> None:
    result = await GetCommitDiffTool().invoke(
        {"project": "main", "sha": "abc1234"}, run_date_iso="2026-05-22"
    )
    assert result.kind is ToolErrorKind.VALIDATION  # type: ignore[union-attr]


async def test_list_branches_rejects_unknown_project() -> None:
    result = await ListBranchesTool().invoke({"project": "nope/nada"}, run_date_iso="2026-05-22")
    assert result.kind is ToolErrorKind.VALIDATION  # type: ignore[union-attr]
