"""Gitlab tab — projects from the discovery registry + cached commits.

The page (`GET /gitlab`) renders off local storage only — no live GitLab call.
`POST /gitlab/refresh` calls `fetch_commits` (monkeypatched to a canned
`(commits, manifest)`) and persists it, then re-renders the fragment.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tl_agent.models import CollectionManifest, GitCommit, ProjectCoverage, UnconfiguredAuthor
from tl_agent.storage import connect, initialize
from tl_agent.storage.repos import gitlab_projects
from tl_agent.web.routes import gitlab as gl


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    db_path = tmp_path / "gl.db"
    monkeypatch.setenv("TLA_SQLITE_PATH", str(db_path))
    conn = connect(db_path)
    initialize(conn)
    # Seed the discovery registry as the startup pass would.
    gitlab_projects.upsert_seen(conn, "tl-agent", ["tl-agent/demo", "tl-agent/infra"])
    conn.commit()
    conn.close()

    from tl_agent.web.app import app

    yield TestClient(app)


def test_gitlab_page_lists_registry_without_fetch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("page render must not call fetch_commits")

    monkeypatch.setattr(gl, "fetch_commits", _boom)

    r = client.get("/gitlab?date=2026-05-22")
    assert r.status_code == 200
    assert "tl-agent/demo" in r.text
    assert "tl-agent/infra" in r.text
    assert "Refresh from GitLab" in r.text


def test_gitlab_refresh_persists_and_renders(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = CollectionManifest(
        gitlab_groups=("tl-agent",),
        projects=[ProjectCoverage(project="tl-agent/demo", searched=True, commit_count=1)],
        unconfigured_authors=[
            UnconfiguredAuthor(
                author="outsider@other.local",
                project="tl-agent/demo",
                commit_count=2,
                sample_sha="0ut51d3abc",
            )
        ],
    )
    commit = GitCommit(
        sha="abcdef1234567",
        project="tl-agent/demo",
        author="john",
        committed_at=datetime(2026, 5, 22, 9, 30, tzinfo=UTC),
        message="fix publisher retry [ENG-1]",
        files_changed=2,
        insertions=10,
        deletions=3,
    )

    async def _fake_fetch(
        team: object,
        since: datetime,
        until: datetime,
        run_date_iso: str,
        notes: list[str],
        conn: object = None,
    ) -> tuple[list[GitCommit], CollectionManifest]:
        return [commit], manifest

    monkeypatch.setattr(gl, "fetch_commits", _fake_fetch)

    r = client.post("/gitlab/refresh", data={"date": "2026-05-22"})
    assert r.status_code == 200
    assert "abcdef12" in r.text  # commit cached + shown under its project
    assert "outsider@other.local" in r.text
    assert "Unconfigured contributors" in r.text

    # Persisted: re-opening the page shows the cached commit without a fetch.
    monkeypatch.setattr(
        gl,
        "fetch_commits",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch on GET")),
    )
    r2 = client.get("/gitlab?date=2026-05-22")
    assert r2.status_code == 200
    assert "abcdef12" in r2.text
