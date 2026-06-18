"""Unit tests for the collection-cache storage layer.

Real SQLite in a tmp dir (per project convention — never mock the DB). Covers
the discovery registry, the per-date commit cache, and the per-date
collection-state row.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tl_agent.models import GitCommit
from tl_agent.models.signals import CollectionManifest, ProjectCoverage, UnconfiguredAuthor
from tl_agent.storage import connect, initialize
from tl_agent.storage.repos import collection_state, gitlab_projects
from tl_agent.storage.repos import commits as commits_repo


@pytest.fixture
def conn(tmp_path: Path):  # type: ignore[no-untyped-def]
    c = connect(tmp_path / "t.db")
    initialize(c)
    yield c
    c.close()


def _commit(sha: str, project: str, author: str = "john") -> GitCommit:
    return GitCommit(
        sha=sha,
        project=project,
        author=author,
        committed_at=datetime(2026, 6, 18, 10, 0, tzinfo=UTC),
        message="m",
        files_changed=1,
        insertions=2,
        deletions=0,
    )


def test_schema_version_is_2(conn) -> None:  # type: ignore[no-untyped-def]
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    assert row["value"] == "2"


def test_gitlab_projects_upsert_and_removed(conn) -> None:  # type: ignore[no-untyped-def]
    gitlab_projects.upsert_seen(conn, "grp", ["grp/a", "grp/b"])
    assert gitlab_projects.active_paths(conn) == ["grp/a", "grp/b"]

    # A later pass no longer sees grp/b → it's flagged removed, not deleted.
    gitlab_projects.upsert_seen(conn, "grp", ["grp/a"])
    gitlab_projects.mark_removed(conn, "grp", ["grp/a"])
    assert gitlab_projects.active_paths(conn) == ["grp/a"]

    # It comes back on a subsequent pass → un-removed.
    gitlab_projects.upsert_seen(conn, "grp", ["grp/a", "grp/b"])
    gitlab_projects.mark_removed(conn, "grp", ["grp/a", "grp/b"])
    assert gitlab_projects.active_paths(conn) == ["grp/a", "grp/b"]


def test_commits_replace_is_idempotent(conn) -> None:  # type: ignore[no-untyped-def]
    d = date(2026, 6, 18)
    commits_repo.replace_for_date(
        conn, d, [_commit("aaaaaaa", "grp/a"), _commit("bbbbbbb", "grp/b")]
    )
    assert commits_repo.count_for_date(conn, d) == 2

    # Replace shrinks the set — no stale rows survive.
    commits_repo.replace_for_date(conn, d, [_commit("aaaaaaa", "grp/a")])
    assert commits_repo.count_for_date(conn, d) == 1
    assert commits_repo.list_for_project(conn, d, "grp/b") == []
    assert commits_repo.list_for_project(conn, d, "grp/a")[0].sha == "aaaaaaa"


def test_collection_state_jira_and_gitlab_dont_clobber(conn) -> None:  # type: ignore[no-untyped-def]
    d = date(2026, 6, 18)
    manifest = CollectionManifest(
        gitlab_groups=("grp",),
        projects=[ProjectCoverage(project="grp/a", searched=True, commit_count=1)],
        unconfigured_authors=[
            UnconfiguredAuthor(author="x@y", project="grp/a", commit_count=1, sample_sha="abc1234")
        ],
    )
    collection_state.set_jira(
        conn, d, sprint_id="S1", sprint_day=3, sprint_length=10, tickets_count=42
    )
    collection_state.set_gitlab(conn, d, manifest=manifest, commits_count=7)

    st = collection_state.get(conn, d)
    assert st is not None
    # Both writes survive each other.
    assert st.sprint_id == "S1" and st.tickets_count == 42 and st.sprint_day == 3
    assert st.commits_count == 7
    assert st.manifest is not None
    assert st.manifest.projects[0].project == "grp/a"
    assert st.jira_collected_at is not None and st.gitlab_collected_at is not None


def test_collection_state_missing_is_none(conn) -> None:  # type: ignore[no-untyped-def]
    assert collection_state.get(conn, date(2026, 6, 18)) is None
