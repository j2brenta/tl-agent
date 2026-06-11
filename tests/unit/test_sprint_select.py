"""Sprint discovery + the human-gate decision (board + sprint).

The selection function reaches into `ctx.team`, `ctx.run_date`, `ctx.notes`,
and `ctx.sqlite` (for the resolved-board cache), so we drive it with a
lightweight context stand-in backed by an in-memory DB and stub the Jira HTTP
via pytest-httpx.
"""

from __future__ import annotations

import re
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from tl_agent.phases import sprint_select
from tl_agent.storage import TeamConfig, connect, initialize
from tl_agent.storage.repos import resolved_config
from tl_agent.tools import registry
from tl_agent.tools.jira import register_jira_tools

_BOARD_SPRINTS_URL = re.compile(r"http://localhost:9100/rest/agile/1.0/board/ENG/sprint")
_BOARDS_URL = re.compile(r"http://localhost:9100/rest/agile/1.0/board(\?|$)")


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    registry.reset()
    register_jira_tools()


def _ctx(
    board_id: str | None = "ENG",
    pattern: str | None = "Eng Sprint .*",
    *,
    cached_board: str | None = None,
) -> Any:
    team = TeamConfig(members=(), board_id=board_id, sprint_name_pattern=pattern)
    conn = connect(":memory:")
    initialize(conn)
    if cached_board:
        resolved_config.set(conn, resolved_config.JIRA_BOARD_KEY, cached_board)
    return SimpleNamespace(
        team=team, run_date=date(2026, 5, 22), run_date_iso="2026-05-22", notes=[], sqlite=conn
    )


def _sprint(sid: str, name: str, state: str, **extra: Any) -> dict[str, Any]:
    return {"id": sid, "name": name, "state": state, "board_id": "ENG", **extra}


def _board(httpx_mock: HTTPXMock, sprints: list[dict[str, Any]]) -> None:
    httpx_mock.add_response(url=_BOARD_SPRINTS_URL, json={"values": sprints})


def _boards(httpx_mock: HTTPXMock, boards: list[dict[str, Any]]) -> None:
    httpx_mock.add_response(url=_BOARDS_URL, json={"values": boards})


async def test_single_active_in_scope_auto_selects(httpx_mock: HTTPXMock) -> None:
    _board(
        httpx_mock,
        [
            _sprint("S-2026-04", "Eng Sprint 18", "closed"),
            _sprint("S-2026-05", "Eng Sprint 19", "active"),
            _sprint("S-2026-06", "Eng Sprint 20", "future"),
        ],
    )
    sel = await sprint_select.run(_ctx())
    assert sel.state == "auto"
    assert sel.chosen_sprint_id == "S-2026-05"
    # Discovery asks the board for active sprints only.
    assert httpx_mock.get_requests()[0].url.params["state"] == "active"


async def test_multiple_active_matches_gates(httpx_mock: HTTPXMock) -> None:
    _board(
        httpx_mock,
        [
            _sprint("S-2026-05", "Eng Sprint 19", "active"),
            _sprint("S-2026-05b", "Eng Sprint 19b", "active"),
        ],
    )
    sel = await sprint_select.run(_ctx())
    assert sel.state == "pending"
    assert {c["id"] for c in sel.candidates} == {"S-2026-05", "S-2026-05b"}


async def test_no_active_match_gates(httpx_mock: HTTPXMock) -> None:
    # Active sprints exist but none match the team scope; gate for a human and
    # offer the active sprints to pick from. The future one is not considered.
    _board(
        httpx_mock,
        [
            _sprint("OPS-1", "Ops Sprint 4", "active"),
            _sprint("S-2026-06", "Eng Sprint 20", "future"),
        ],
    )
    sel = await sprint_select.run(_ctx())
    assert sel.state == "pending"
    assert {c["id"] for c in sel.candidates} == {"OPS-1"}


async def test_no_active_sprints_at_all_gates_with_no_candidates(httpx_mock: HTTPXMock) -> None:
    # Between sprints — only closed/future exist, nothing active to operate over.
    _board(
        httpx_mock,
        [
            _sprint("S-2026-04", "Eng Sprint 18", "closed"),
            _sprint("S-2026-06", "Eng Sprint 20", "future"),
        ],
    )
    sel = await sprint_select.run(_ctx())
    assert sel.state == "pending"
    assert sel.candidates == []


async def test_configured_board_skips_discovery(httpx_mock: HTTPXMock) -> None:
    _board(httpx_mock, [_sprint("S-2026-05", "Eng Sprint 19", "active")])
    sel = await sprint_select.run(_ctx())  # board_id="ENG" configured
    assert sel.state == "auto"
    # Only the board-sprints endpoint was hit — no board *discovery* call.
    assert all("/board/ENG/sprint" in str(r.url) for r in httpx_mock.get_requests())


# -------------------- board discovery --------------------


async def test_single_discovered_board_is_used_and_cached(httpx_mock: HTTPXMock) -> None:
    _boards(httpx_mock, [{"id": "ENG", "name": "Engineering"}])
    _board(httpx_mock, [_sprint("S-2026-05", "Eng Sprint 19", "active")])
    ctx = _ctx(board_id=None)
    sel = await sprint_select.run(ctx)
    assert sel.state == "auto"
    assert sel.chosen_sprint_id == "S-2026-05"
    # The discovered board is cached so future runs skip discovery.
    assert resolved_config.get(ctx.sqlite, resolved_config.JIRA_BOARD_KEY) == "ENG"


async def test_multiple_boards_gate_for_human(httpx_mock: HTTPXMock) -> None:
    _boards(httpx_mock, [{"id": "ENG", "name": "Engineering"}, {"id": "OPS", "name": "Ops"}])
    sel = await sprint_select.run(_ctx(board_id=None))
    assert sel.state == "pending"
    assert sel.kind == "board"
    assert {c["id"] for c in sel.candidates} == {"ENG", "OPS"}


async def test_cached_board_skips_discovery(httpx_mock: HTTPXMock) -> None:
    # A previously resolved board in the DB is used without a discovery call.
    _board(httpx_mock, [_sprint("S-2026-05", "Eng Sprint 19", "active")])
    sel = await sprint_select.run(_ctx(board_id=None, cached_board="ENG"))
    assert sel.state == "auto"
    assert sel.chosen_sprint_id == "S-2026-05"
    assert all("/rest/agile/1.0/board/ENG/sprint" in str(r.url) for r in httpx_mock.get_requests())


async def test_no_boards_discovered_degrades_to_auto(httpx_mock: HTTPXMock) -> None:
    _boards(httpx_mock, [])
    sel = await sprint_select.run(_ctx(board_id=None))
    assert sel.state == "auto"
    assert sel.chosen_sprint_id is None


async def test_missing_pattern_falls_back_to_active(httpx_mock: HTTPXMock) -> None:
    # Board known but no team filter configured → degrade, don't guess.
    sel = await sprint_select.run(_ctx(pattern=None))
    assert sel.state == "auto"
    assert sel.chosen_sprint_id is None
