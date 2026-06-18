"""collection_state repo — one row per run_date describing what's cached.

Drives the Run-now "what's cached" summary and lets a "reuse stored" run
rebuild the parts of `DailySignals` that aren't reconstructable from the other
caches (sprint progress + the GitLab collection manifest). Jira and GitLab are
collected by separate buttons, so `set_jira`/`set_gitlab` each touch only their
own columns — neither clobbers the other side.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime

from tl_agent.models.signals import CollectionManifest
from tl_agent.storage.repos._base import from_json, to_json


@dataclass(frozen=True)
class CollectionState:
    run_date: str
    jira_collected_at: str | None
    gitlab_collected_at: str | None
    sprint_id: str | None
    sprint_day: int | None
    sprint_length: int | None
    tickets_count: int | None
    commits_count: int | None
    manifest: CollectionManifest | None


def _ensure_row(conn: sqlite3.Connection, run_date_iso: str) -> None:
    conn.execute(
        "INSERT INTO collection_state (run_date) VALUES (?) ON CONFLICT(run_date) DO NOTHING",
        (run_date_iso,),
    )


def set_jira(
    conn: sqlite3.Connection,
    run_date: date,
    *,
    sprint_id: str | None,
    sprint_day: int | None,
    sprint_length: int | None,
    tickets_count: int,
) -> None:
    run_date_iso = run_date.isoformat()
    _ensure_row(conn, run_date_iso)
    conn.execute(
        """
        UPDATE collection_state SET
            jira_collected_at = ?,
            sprint_id         = ?,
            sprint_day        = ?,
            sprint_length     = ?,
            tickets_count     = ?
        WHERE run_date = ?
        """,
        (
            datetime.now(UTC).isoformat(),
            sprint_id,
            sprint_day,
            sprint_length,
            tickets_count,
            run_date_iso,
        ),
    )


def set_gitlab(
    conn: sqlite3.Connection,
    run_date: date,
    *,
    manifest: CollectionManifest,
    commits_count: int,
) -> None:
    run_date_iso = run_date.isoformat()
    _ensure_row(conn, run_date_iso)
    conn.execute(
        """
        UPDATE collection_state SET
            gitlab_collected_at = ?,
            commits_count       = ?,
            manifest_json       = ?
        WHERE run_date = ?
        """,
        (
            datetime.now(UTC).isoformat(),
            commits_count,
            to_json(manifest.model_dump(mode="json")),
            run_date_iso,
        ),
    )


def get(conn: sqlite3.Connection, run_date: date) -> CollectionState | None:
    row = conn.execute(
        "SELECT * FROM collection_state WHERE run_date = ?",
        (run_date.isoformat(),),
    ).fetchone()
    if row is None:
        return None
    manifest = (
        CollectionManifest.model_validate(from_json(row["manifest_json"]))
        if row["manifest_json"]
        else None
    )
    return CollectionState(
        run_date=row["run_date"],
        jira_collected_at=row["jira_collected_at"],
        gitlab_collected_at=row["gitlab_collected_at"],
        sprint_id=row["sprint_id"],
        sprint_day=row["sprint_day"],
        sprint_length=row["sprint_length"],
        tickets_count=row["tickets_count"],
        commits_count=row["commits_count"],
        manifest=manifest,
    )
