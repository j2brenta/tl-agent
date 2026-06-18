"""collected_commits repo — per-date cache of GitLab commits.

Whatever the collection window pulled for a run_date is stored here so the
Gitlab tab and a "reuse stored" pipeline run can read commits back without
re-hitting GitLab. `replace_for_date` is delete-then-insert: a re-collect for
the same date fully refreshes the cache (commits never leave a window, so a
straight replace is correct and avoids stale rows).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import date

from tl_agent.models import GitCommit
from tl_agent.storage.repos._base import from_json, to_json


def replace_for_date(
    conn: sqlite3.Connection, run_date: date, commits: Iterable[GitCommit]
) -> None:
    run_date_iso = run_date.isoformat()
    conn.execute("DELETE FROM collected_commits WHERE run_date = ?", (run_date_iso,))
    for c in commits:
        conn.execute(
            """
            INSERT INTO collected_commits
                (run_date, project, sha, author, committed_at, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_date, project, sha) DO UPDATE SET
                author       = excluded.author,
                committed_at = excluded.committed_at,
                payload      = excluded.payload
            """,
            (
                run_date_iso,
                c.project,
                c.sha,
                c.author,
                c.committed_at.isoformat(),
                to_json(c.model_dump(mode="json")),
            ),
        )


def list_for_date(conn: sqlite3.Connection, run_date: date) -> list[GitCommit]:
    rows = conn.execute(
        "SELECT payload FROM collected_commits WHERE run_date = ? ORDER BY committed_at DESC",
        (run_date.isoformat(),),
    ).fetchall()
    return [GitCommit.model_validate(from_json(r["payload"])) for r in rows]


def list_for_project(conn: sqlite3.Connection, run_date: date, project: str) -> list[GitCommit]:
    rows = conn.execute(
        """
        SELECT payload FROM collected_commits
        WHERE run_date = ? AND project = ?
        ORDER BY committed_at DESC
        """,
        (run_date.isoformat(), project),
    ).fetchall()
    return [GitCommit.model_validate(from_json(r["payload"])) for r in rows]


def count_for_date(conn: sqlite3.Connection, run_date: date) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM collected_commits WHERE run_date = ?",
        (run_date.isoformat(),),
    ).fetchone()
    return int(row["n"]) if row else 0
