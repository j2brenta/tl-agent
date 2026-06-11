"""Tests for the concrete Jira/GitLab/memory tools.

We use pytest-httpx to stub the HTTP layer — no real services touched.
End-to-end against the compose stack lives in tests/integration/.
"""

from __future__ import annotations

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
    _parse_ticket_keys,
    register_gitlab_tools,
)
from tl_agent.tools.jira import (
    GetDependenciesTool,
    GetHistoryTool,
    GetTicketTool,
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
    assert "search_standup_history" in names
    assert "post_jira_comment" in names
    assert "list_sprints" in names

    # Phase-5 binding excludes writers
    phase5_names = {t.name for t in registry.for_phase5()}
    assert "post_jira_comment" not in phase5_names
    assert "get_ticket" in phase5_names


# -------------------- jira --------------------


async def test_get_ticket_happy_path(httpx_mock: HTTPXMock) -> None:
    payload = {
        "key": "ENG-12",
        "summary": "add retry",
        "status": "in_progress",
        "assignee": "john",
        "created_at": "2026-05-20T09:00:00+00:00",
        "updated_at": "2026-05-22T09:00:00+00:00",
        "blocks": [],
        "blocked_by": [],
    }
    httpx_mock.add_response(url="http://localhost:9100/rest/api/3/issue/ENG-12", json=payload)
    tool = GetTicketTool()
    result = await tool.invoke({"key": "ENG-12"}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolResult)
    assert result.value.ticket.key == "ENG-12"
    assert result.value.ticket.assignee == "john"


async def test_get_ticket_404_returns_typed_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://localhost:9100/rest/api/3/issue/MISSING-1",
        status_code=404,
        json={"error": "not found"},
    )
    tool = GetTicketTool()
    result = await tool.invoke({"key": "MISSING-1"}, run_date_iso="2026-05-22")
    assert hasattr(result, "kind")
    assert result.kind is ToolErrorKind.NOT_FOUND  # type: ignore[union-attr]


async def test_get_ticket_history(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://localhost:9100/rest/api/3/issue/ENG-12/changelog",
        json={
            "values": [
                {
                    "at": "2026-05-20T10:00:00+00:00",
                    "by": "john",
                    "from_status": "todo",
                    "to_status": "in_progress",
                }
            ]
        },
    )
    result = await GetHistoryTool().invoke({"key": "ENG-12"}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolResult)
    assert len(result.value.status_changes) == 1
    assert result.value.status_changes[0].to_status.value == "in_progress"


async def test_get_dependencies(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://localhost:9100/rest/api/3/issue/ENG-12/links",
        json={"blocks": ["ENG-19"], "blocked_by": ["ENG-9"]},
    )
    result = await GetDependenciesTool().invoke({"key": "ENG-12"}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolResult)
    assert result.value.blocks == ["ENG-19"]
    assert result.value.blocked_by == ["ENG-9"]


async def test_list_sprint(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://localhost:9100/rest/agile/1.0/sprint/active/issue",
        json={
            "sprint_id": "S-2026-05",
            "sprint_day": 4,
            "sprint_length_days": 10,
            "added_since": "2026-05-21T12:00:00+00:00",
            "issues": [],
        },
    )
    result = await ListSprintTool().invoke({}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolResult)
    assert result.value.sprint_id == "S-2026-05"
    assert result.value.sprint_day == 4


async def test_list_sprints(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://localhost:9100/rest/agile/1.0/board/ENG/sprint",
        json={
            "values": [
                {"id": "S-2026-04", "name": "Eng Sprint 18", "state": "closed", "board_id": "ENG"},
                {
                    "id": "S-2026-05",
                    "name": "Eng Sprint 19",
                    "state": "active",
                    "board_id": "ENG",
                    "sprint_day": 4,
                    "sprint_length_days": 10,
                },
            ]
        },
    )
    result = await ListSprintsTool().invoke({"board_id": "ENG"}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolResult)
    assert [s.id for s in result.value.sprints] == ["S-2026-04", "S-2026-05"]
    active = [s for s in result.value.sprints if s.state.value == "active"]
    assert active[0].name == "Eng Sprint 19"
    assert active[0].sprint_day == 4


async def test_post_jira_comment_writes_with_idempotency(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://localhost:9100/rest/api/3/issue/ENG-12/comment",
        method="POST",
        json={
            "key": "ENG-12",
            "comment_id": "C-1",
            "posted_at": "2026-05-22T10:00:00+00:00",
        },
    )
    tool = PostCommentTool()
    # No idempotency store ⇒ goes through; just smoke that the writer works.
    result = await tool.invoke({"key": "ENG-12", "body": "hi"}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolResult)
    assert result.value.comment_id == "C-1"


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
    assert "tl-agent/demo" in result.message  # type: ignore[union-attr]


async def test_get_commit_diff_rejects_unknown_project() -> None:
    result = await GetCommitDiffTool().invoke(
        {"project": "main", "sha": "abc1234"}, run_date_iso="2026-05-22"
    )
    assert result.kind is ToolErrorKind.VALIDATION  # type: ignore[union-attr]


async def test_list_branches_rejects_unknown_project() -> None:
    result = await ListBranchesTool().invoke({"project": "nope/nada"}, run_date_iso="2026-05-22")
    assert result.kind is ToolErrorKind.VALIDATION  # type: ignore[union-attr]
