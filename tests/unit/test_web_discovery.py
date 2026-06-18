"""Discovery tab — renders GitLab coverage + unconfigured contributors.

The route calls `fetch_commits` (live GitLab); we monkeypatch it to a canned
`(commits, manifest)` so the render is deterministic and network-free.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tl_agent.models import CollectionManifest, ProjectCoverage, UnconfiguredAuthor
from tl_agent.storage import connect, initialize
from tl_agent.web.routes import discovery as disc


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    db_path = tmp_path / "disc.db"
    monkeypatch.setenv("TLA_SQLITE_PATH", str(db_path))
    conn = connect(db_path)
    initialize(conn)
    conn.close()

    from tl_agent.web.app import app

    yield TestClient(app)


def test_discovery_page_lists_projects_and_unconfigured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = CollectionManifest(
        gitlab_groups=("tl-agent",),
        used_fallback=False,
        projects=[ProjectCoverage(project="tl-agent/demo", searched=True, commit_count=4)],
        unconfigured_authors=[
            UnconfiguredAuthor(
                author="outsider@other.local",
                project="tl-agent/demo",
                commit_count=2,
                sample_sha="0ut51d3abc",
            )
        ],
    )

    async def _fake_fetch(
        team: object, since: datetime, until: datetime, run_date_iso: str, notes: list[str]
    ) -> tuple[list[object], CollectionManifest]:
        return [], manifest

    monkeypatch.setattr(disc, "fetch_commits", _fake_fetch)

    r = client.get("/discovery?date=2026-05-22")
    assert r.status_code == 200
    assert "tl-agent/demo" in r.text
    assert "outsider@other.local" in r.text
    assert "Unconfigured contributors" in r.text
    assert "0ut51d3a" in r.text  # short sample sha


def test_discovery_page_clean_when_all_resolve(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = CollectionManifest(
        gitlab_groups=("tl-agent",),
        projects=[ProjectCoverage(project="tl-agent/demo", searched=True, commit_count=4)],
    )

    async def _fake_fetch(
        team: object, since: datetime, until: datetime, run_date_iso: str, notes: list[str]
    ) -> tuple[list[object], CollectionManifest]:
        return [], manifest

    monkeypatch.setattr(disc, "fetch_commits", _fake_fetch)

    r = client.get("/discovery?date=2026-05-22")
    assert r.status_code == 200
    assert "resolves to someone on the roster" in r.text
