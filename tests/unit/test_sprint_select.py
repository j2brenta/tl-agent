"""Sprint discovery + the human-gate decision.

The selection function only reaches into `ctx.team.board_id`,
`ctx.team.sprint_name_pattern`, `ctx.run_date_iso` and `ctx.notes`, so we drive
it with a lightweight context stand-in and stub the board HTTP via pytest-httpx.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from tl_agent.phases import sprint_select
from tl_agent.storage import TeamConfig
from tl_agent.tools import registry
from tl_agent.tools.jira import register_jira_tools

_BOARD_URL = "http://localhost:9100/rest/agile/1.0/board/ENG/sprint"


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    registry.reset()
    register_jira_tools()


def _ctx(board_id: str | None = "ENG", pattern: str | None = "Eng Sprint .*") -> Any:
    team = TeamConfig(members=(), board_id=board_id, sprint_name_pattern=pattern)
    return SimpleNamespace(team=team, run_date_iso="2026-05-22", notes=[])


def _sprint(sid: str, name: str, state: str, **extra: Any) -> dict[str, Any]:
    return {"id": sid, "name": name, "state": state, "board_id": "ENG", **extra}


def _board(httpx_mock: HTTPXMock, sprints: list[dict[str, Any]]) -> None:
    httpx_mock.add_response(url=_BOARD_URL, json={"values": sprints})


async def test_single_active_in_scope_auto_selects(httpx_mock: HTTPXMock) -> None:
    _board(
        httpx_mock,
        [
            _sprint("S-2026-04", "Eng Sprint 18", "closed"),
            _sprint("S-2026-05", "Eng Sprint 19", "active", sprint_day=4, sprint_length_days=10),
            _sprint("S-2026-06", "Eng Sprint 20", "future"),
        ],
    )
    sel = await sprint_select.run(_ctx())
    assert sel.state == "auto"
    assert sel.chosen_sprint_id == "S-2026-05"


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
    # Active sprint exists but its name is out of scope; the in-scope one is future.
    _board(
        httpx_mock,
        [
            _sprint("OPS-1", "Ops Sprint 4", "active"),
            _sprint("S-2026-06", "Eng Sprint 20", "future"),
        ],
    )
    sel = await sprint_select.run(_ctx())
    assert sel.state == "pending"
    # The future in-scope sprint is offered as a candidate.
    assert "S-2026-06" in {c["id"] for c in sel.candidates}


async def test_closed_sprints_are_never_in_scope(httpx_mock: HTTPXMock) -> None:
    _board(httpx_mock, [_sprint("S-2026-04", "Eng Sprint 18", "closed")])
    sel = await sprint_select.run(_ctx())
    assert sel.state == "pending"
    assert all(c["state"] != "closed" for c in sel.candidates) or sel.candidates == []


async def test_missing_config_falls_back_to_active() -> None:
    sel = await sprint_select.run(_ctx(board_id=None, pattern=None))
    assert sel.state == "auto"
    assert sel.chosen_sprint_id is None
