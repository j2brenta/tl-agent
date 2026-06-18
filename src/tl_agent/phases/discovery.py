"""Project discovery — resolve the team's GitLab projects and persist them.

Runs as a background task on web startup (and re-runs on each startup), so the
Gitlab tab can list "projects we already know about" instantly without a live
GitLab call, and a pipeline run reuses the registry instead of re-discovering.

Each pass upserts what each configured group currently returns and flips
`removed = 1` for paths a group no longer reports — so the registry tracks
"what changed" across deployments rather than growing monotonically.

When `team.gitlab_groups` is empty we fall back to the static allow-list in
`config/gitlab_projects.yaml`, recorded under the synthetic group `''` (mirrors
`phase1_collect._discover_projects`'s fallback).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from tl_agent.storage import TeamConfig
from tl_agent.storage.markdown_loader import load_allowed_gitlab_projects
from tl_agent.storage.repos import gitlab_projects
from tl_agent.tools import ToolResult
from tl_agent.tools.gitlab import ListGroupProjectsTool

logger = logging.getLogger(__name__)

_FALLBACK_GROUP = ""


@dataclass(frozen=True)
class DiscoveryResult:
    total: int
    groups: int
    used_fallback: bool


async def discover_and_persist(conn: sqlite3.Connection, team: TeamConfig) -> DiscoveryResult:
    """Discover projects under each configured group and persist the registry."""
    if not team.gitlab_groups:
        paths = sorted(load_allowed_gitlab_projects())
        gitlab_projects.upsert_seen(conn, _FALLBACK_GROUP, paths)
        gitlab_projects.mark_removed(conn, _FALLBACK_GROUP, paths)
        conn.commit()
        logger.info(
            "discovery.persisted",
            extra={"used_fallback": True, "total": len(paths)},
        )
        return DiscoveryResult(total=len(paths), groups=0, used_fallback=True)

    tool = ListGroupProjectsTool()
    total = 0
    for group in team.gitlab_groups:
        result = await tool.invoke({"group": group}, run_date_iso="")
        if not isinstance(result, ToolResult):
            logger.warning(
                "discovery.group_failed",
                extra={"group": group, "kind": result.kind.value, "error_detail": result.message},
            )
            continue
        paths = list(result.value.projects)
        gitlab_projects.upsert_seen(conn, group, paths)
        gitlab_projects.mark_removed(conn, group, paths)
        total += len(paths)
    conn.commit()
    logger.info(
        "discovery.persisted",
        extra={"groups": len(team.gitlab_groups), "total": total, "used_fallback": False},
    )
    return DiscoveryResult(total=total, groups=len(team.gitlab_groups), used_fallback=False)
