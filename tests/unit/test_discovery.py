"""Background project discovery — persists the GitLab project registry.

`discover_and_persist` lists each configured group's projects and upserts them
into `gitlab_projects`, flagging gone paths as removed. HTTP is stubbed with
pytest-httpx; the DB is real SQLite in a tmp dir.
"""

from __future__ import annotations

import re
from pathlib import Path

from pytest_httpx import HTTPXMock

from tl_agent.phases.discovery import discover_and_persist
from tl_agent.storage import connect, initialize, load_team
from tl_agent.storage.repos import gitlab_projects


async def test_discover_persists_registry(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*/groups/tl-agent/projects.*"),
        json=[
            {"path_with_namespace": "tl-agent/demo"},
            {"path_with_namespace": "tl-agent/infra"},
        ],
    )
    conn = connect(tmp_path / "d.db")
    initialize(conn)
    try:
        result = await discover_and_persist(conn, load_team())
        assert result.total == 2
        assert gitlab_projects.active_paths(conn) == ["tl-agent/demo", "tl-agent/infra"]
    finally:
        conn.close()


async def test_discover_flags_removed_project(httpx_mock: HTTPXMock, tmp_path: Path) -> None:
    conn = connect(tmp_path / "d.db")
    initialize(conn)
    # First pass sees two projects.
    gitlab_projects.upsert_seen(conn, "tl-agent", ["tl-agent/demo", "tl-agent/old"])
    conn.commit()

    # Discovery now returns only one — the other must be flagged removed.
    httpx_mock.add_response(
        url=re.compile(r".*/groups/tl-agent/projects.*"),
        json=[{"path_with_namespace": "tl-agent/demo"}],
    )
    try:
        await discover_and_persist(conn, load_team())
        assert gitlab_projects.active_paths(conn) == ["tl-agent/demo"]
    finally:
        conn.close()
