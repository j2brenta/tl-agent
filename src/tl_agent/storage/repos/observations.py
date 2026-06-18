"""standup_observations repo — FTS5-backed standup history.

`search` is the tool the agent uses in Phase 5 (`search_standup_history`).
FTS5 query syntax is exposed verbatim — Phase 5's prompt is told to use it.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import NamedTuple


class StoredObservation(NamedTuple):
    """One row in standup_observations."""

    id: str
    run_date: date
    engineer_id: str
    raw: str
    summary: str | None
    chat_message_id: str | None


class SearchHit(NamedTuple):
    """One FTS5 search hit, ranked by bm25."""

    run_date: date
    engineer_id: str
    raw: str
    summary: str | None
    rank: float  # bm25 score; lower is better


def upsert(
    conn: sqlite3.Connection,
    *,
    obs_id: str,
    run_date: date,
    engineer_id: str,
    raw: str,
    summary: str | None,
    chat_message_id: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO standup_observations (
            id, run_date, engineer_id, raw, summary, chat_message_id
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_date, engineer_id) DO UPDATE SET
            raw = excluded.raw,
            summary = excluded.summary,
            chat_message_id = excluded.chat_message_id
        """,
        (obs_id, run_date.isoformat(), engineer_id, raw, summary, chat_message_id),
    )


def get(conn: sqlite3.Connection, *, run_date: date, engineer_id: str) -> StoredObservation | None:
    row = conn.execute(
        "SELECT * FROM standup_observations WHERE run_date = ? AND engineer_id = ?",
        (run_date.isoformat(), engineer_id),
    ).fetchone()
    if not row:
        return None
    return StoredObservation(
        id=row["id"],
        run_date=date.fromisoformat(row["run_date"]),
        engineer_id=row["engineer_id"],
        raw=row["raw"],
        summary=row["summary"],
        chat_message_id=row["chat_message_id"],
    )


def list_for_date(conn: sqlite3.Connection, run_date: date) -> list[StoredObservation]:
    """Every stored standup observation for one day, ordered by engineer."""
    rows = conn.execute(
        "SELECT * FROM standup_observations WHERE run_date = ? ORDER BY engineer_id",
        (run_date.isoformat(),),
    ).fetchall()
    return [
        StoredObservation(
            id=r["id"],
            run_date=date.fromisoformat(r["run_date"]),
            engineer_id=r["engineer_id"],
            raw=r["raw"],
            summary=r["summary"],
            chat_message_id=r["chat_message_id"],
        )
        for r in rows
    ]


def search(
    conn: sqlite3.Connection,
    *,
    query: str,
    engineer_id: str | None = None,
    days: int | None = None,
    today: date | None = None,
    limit: int = 20,
) -> list[SearchHit]:
    """FTS5 search across raw + summary.

    `query` is FTS5 syntax (`blocker AND publisher`, `"retry policy"`, etc.).
    """
    where = ["standup_observations_fts MATCH ?"]
    params: list[str] = [query]
    if engineer_id:
        where.append("standup_observations.engineer_id = ?")
        params.append(engineer_id)
    if days is not None and today is not None:
        where.append("standup_observations.run_date >= date(?, ?)")
        params.append(today.isoformat())
        params.append(f"-{days} days")
    where_sql = " AND ".join(where)

    sql = f"""
        SELECT
            standup_observations.run_date    AS run_date,
            standup_observations.engineer_id AS engineer_id,
            standup_observations.raw         AS raw,
            standup_observations.summary     AS summary,
            bm25(standup_observations_fts)   AS rank
        FROM standup_observations_fts
        JOIN standup_observations ON standup_observations.rowid = standup_observations_fts.rowid
        WHERE {where_sql}
        ORDER BY rank
        LIMIT ?
    """  # noqa: S608 — `where_sql` is fully derived from hard-coded fragments
    params.append(str(limit))
    rows = conn.execute(sql, params).fetchall()
    return [
        SearchHit(
            run_date=date.fromisoformat(r["run_date"]),
            engineer_id=r["engineer_id"],
            raw=r["raw"],
            summary=r["summary"],
            rank=float(r["rank"]),
        )
        for r in rows
    ]
