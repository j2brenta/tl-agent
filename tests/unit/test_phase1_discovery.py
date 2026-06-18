"""Phase 1 GitLab collection: per-project fetch + author attribution.

`fetch_commits` pulls every commit in each discovered project (one unfiltered
`list_commits` per project) and buckets authors against the roster. A commit by
someone who doesn't resolve to a team member must surface in the manifest's
`unconfigured_authors` — not silently vanish, and not pollute the team-commit
set Phase 2/3 reason over. We stub the HTTP layer with pytest-httpx.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pytest_httpx import HTTPXMock

from tl_agent.phases.phase1_collect import fetch_commits
from tl_agent.storage import load_team

_SINCE = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
_UNTIL = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


async def test_unconfigured_author_surfaces_in_manifest(httpx_mock: HTTPXMock) -> None:
    # Group discovery → one project.
    httpx_mock.add_response(
        url=re.compile(r".*/groups/tl-agent/projects.*"),
        json=[{"path_with_namespace": "tl-agent/demo"}],
    )
    # Project commits → one roster member (john) and one off-team author.
    httpx_mock.add_response(
        url=re.compile(r".*/repository/commits.*"),
        json=[
            {
                "id": "j0b1aaaaaaaa",
                "author_email": "john@example.local",
                "committed_date": "2026-05-22T09:00:00Z",
                "message": "ENG-12 fix retry",
                "stats": {"files": 2, "additions": 10, "deletions": 3},
            },
            {
                "id": "0ut51d3bbbbb",
                "author_email": "outsider@other.local",
                "author_name": "Outside Dev",
                "committed_date": "2026-05-22T08:00:00Z",
                "message": "drive-by hotfix",
                "stats": {"files": 1, "additions": 4, "deletions": 0},
            },
        ],
    )

    notes: list[str] = []
    commits, manifest = await fetch_commits(load_team(), _SINCE, _UNTIL, "2026-05-22", notes)

    # Team-commit set: only the roster member's commit.
    assert [c.author for c in commits] == ["john@example.local"]

    # Manifest: project searched, both commits counted in coverage.
    assert manifest.gitlab_groups == ("tl-agent",)
    assert manifest.used_fallback is False
    assert len(manifest.projects) == 1
    cov = manifest.projects[0]
    assert cov.project == "tl-agent/demo"
    assert cov.searched is True
    assert cov.commit_count == 2
    assert cov.error is None

    # The off-team author is flagged, with a count + sample sha.
    assert len(manifest.unconfigured_authors) == 1
    unknown = manifest.unconfigured_authors[0]
    assert unknown.author == "outsider@other.local"
    assert unknown.project == "tl-agent/demo"
    assert unknown.commit_count == 1
    assert unknown.sample_sha.startswith("0ut51d3")


async def test_failed_project_search_recorded_not_raised(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=re.compile(r".*/groups/tl-agent/projects.*"),
        json=[{"path_with_namespace": "tl-agent/demo"}],
    )
    # 404 → NOT_FOUND, which the retry policy does not retry (single response).
    httpx_mock.add_response(url=re.compile(r".*/repository/commits.*"), status_code=404)

    notes: list[str] = []
    commits, manifest = await fetch_commits(load_team(), _SINCE, _UNTIL, "2026-05-22", notes)

    assert commits == []
    assert len(manifest.projects) == 1
    cov = manifest.projects[0]
    assert cov.searched is False
    assert cov.error is not None
    assert manifest.unconfigured_authors == []
    assert any("tl-agent/demo" in n for n in notes)
