"""daily_flags repo — open flags carried day over day."""

from __future__ import annotations

import sqlite3
from datetime import date

from tl_agent.models import Flag, FlagType, TriageStatus
from tl_agent.storage.repos._base import from_json, to_json


def upsert(conn: sqlite3.Connection, flag: Flag, run_date: date) -> None:
    """Insert or update a flag for a given run date."""
    conn.execute(
        """
        INSERT INTO daily_flags (
            id, run_date, engineer_id, type, title, status, days_hot,
            first_seen, last_seen, resolved, resolution_note, related_ticket_ids
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            run_date = excluded.run_date,
            engineer_id = excluded.engineer_id,
            type = excluded.type,
            title = excluded.title,
            status = excluded.status,
            days_hot = excluded.days_hot,
            last_seen = excluded.last_seen,
            resolved = excluded.resolved,
            resolution_note = excluded.resolution_note,
            related_ticket_ids = excluded.related_ticket_ids
        """,
        (
            flag.id,
            run_date.isoformat(),
            flag.engineer_id,
            flag.type.value,
            flag.title,
            flag.status.value,
            flag.days_hot,
            flag.first_seen.isoformat(),
            flag.last_seen.isoformat(),
            int(flag.resolved),
            flag.resolution_note,
            to_json(list(flag.related_ticket_ids)),
        ),
    )


def _row_to_flag(row: sqlite3.Row) -> Flag:
    return Flag(
        id=row["id"],
        type=FlagType(row["type"]),
        title=row["title"],
        engineer_id=row["engineer_id"],
        related_ticket_ids=tuple(from_json(row["related_ticket_ids"])),
        status=TriageStatus(row["status"]),
        days_hot=row["days_hot"],
        first_seen=date.fromisoformat(row["first_seen"]),
        last_seen=date.fromisoformat(row["last_seen"]),
        resolved=bool(row["resolved"]),
        resolution_note=row["resolution_note"],
    )


def list_open_on(conn: sqlite3.Connection, run_date: date) -> list[Flag]:
    """All not-yet-resolved flags as of `run_date`."""
    rows = conn.execute(
        """
        SELECT * FROM daily_flags
        WHERE resolved = 0 AND last_seen <= ?
        ORDER BY days_hot DESC, last_seen DESC
        """,
        (run_date.isoformat(),),
    ).fetchall()
    return [_row_to_flag(r) for r in rows]


def get(conn: sqlite3.Connection, flag_id: str) -> Flag | None:
    """Single flag by id."""
    row = conn.execute("SELECT * FROM daily_flags WHERE id = ?", (flag_id,)).fetchone()
    return _row_to_flag(row) if row else None


def mark_resolved(
    conn: sqlite3.Connection,
    flag_id: str,
    *,
    note: str | None = None,
    resolved_on: date,
) -> None:
    """Close a flag — Phase 0 calls this when yesterday's flag is gone."""
    conn.execute(
        """
        UPDATE daily_flags
        SET resolved = 1, resolution_note = ?, last_seen = ?
        WHERE id = ?
        """,
        (note, resolved_on.isoformat(), flag_id),
    )
