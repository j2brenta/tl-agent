"""gitlab_projects repo — the discovered-project registry.

Populated by the background discovery pass on web startup
(`phases.discovery.discover_and_persist`). Not keyed by date: it's the current
shape of the team's GitLab groups, diffed on each pass. `removed` flips to 1
when a path is no longer returned for its group, so the Gitlab tab can hide
gone repos without losing their history.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class GitlabProject:
    path: str
    group_path: str
    first_seen: str
    last_seen: str
    removed: bool


def upsert_seen(conn: sqlite3.Connection, group: str, paths: Iterable[str]) -> None:
    """Record that `paths` were seen under `group` in this discovery pass.

    Inserts new projects, refreshes `last_seen` and un-removes existing ones.
    """
    now = datetime.now(UTC).isoformat()
    for path in paths:
        conn.execute(
            """
            INSERT INTO gitlab_projects (path, group_path, first_seen, last_seen, removed)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(path) DO UPDATE SET
                group_path = excluded.group_path,
                last_seen  = excluded.last_seen,
                removed    = 0
            """,
            (path, group, now, now),
        )


def mark_removed(conn: sqlite3.Connection, group: str, kept_paths: Iterable[str]) -> None:
    """Flag any project under `group` not in `kept_paths` as removed."""
    kept = set(kept_paths)
    rows = conn.execute(
        "SELECT path FROM gitlab_projects WHERE group_path = ? AND removed = 0",
        (group,),
    ).fetchall()
    gone = [r["path"] for r in rows if r["path"] not in kept]
    for path in gone:
        conn.execute("UPDATE gitlab_projects SET removed = 1 WHERE path = ?", (path,))


def list_active(conn: sqlite3.Connection) -> list[GitlabProject]:
    """All non-removed projects, ordered by path."""
    rows = conn.execute("SELECT * FROM gitlab_projects WHERE removed = 0 ORDER BY path").fetchall()
    return [_row(r) for r in rows]


def active_paths(conn: sqlite3.Connection) -> list[str]:
    """Just the paths of non-removed projects, ordered — for collection fan-out."""
    rows = conn.execute(
        "SELECT path FROM gitlab_projects WHERE removed = 0 ORDER BY path"
    ).fetchall()
    return [r["path"] for r in rows]


def _row(row: sqlite3.Row) -> GitlabProject:
    return GitlabProject(
        path=row["path"],
        group_path=row["group_path"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        removed=bool(row["removed"]),
    )
