"""ticket_snapshots repo — daily Jira ticket snapshots, for delta detection."""

from __future__ import annotations

import sqlite3
from datetime import date

from tl_agent.models import JiraStatus, JiraTicket
from tl_agent.storage.repos._base import from_json, to_json


def upsert(conn: sqlite3.Connection, run_date: date, ticket: JiraTicket) -> None:
    conn.execute(
        """
        INSERT INTO ticket_snapshots (run_date, ticket_id, status, assignee, points, payload)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_date, ticket_id) DO UPDATE SET
            status = excluded.status,
            assignee = excluded.assignee,
            points = excluded.points,
            payload = excluded.payload
        """,
        (
            run_date.isoformat(),
            ticket.key,
            ticket.status.value,
            ticket.assignee,
            ticket.points,
            to_json(ticket.model_dump(mode="json")),
        ),
    )


def get_for_date(conn: sqlite3.Connection, run_date: date, ticket_id: str) -> JiraTicket | None:
    row = conn.execute(
        "SELECT payload FROM ticket_snapshots WHERE run_date = ? AND ticket_id = ?",
        (run_date.isoformat(), ticket_id),
    ).fetchone()
    if not row:
        return None
    return JiraTicket.model_validate(from_json(row["payload"]))


def list_for_date(conn: sqlite3.Connection, run_date: date) -> list[JiraTicket]:
    rows = conn.execute(
        "SELECT payload FROM ticket_snapshots WHERE run_date = ? ORDER BY ticket_id",
        (run_date.isoformat(),),
    ).fetchall()
    return [JiraTicket.model_validate(from_json(r["payload"])) for r in rows]


def list_status_history(
    conn: sqlite3.Connection, ticket_id: str, *, since: date
) -> list[tuple[date, JiraStatus]]:
    """(run_date, status) pairs for one ticket over recent days."""
    rows = conn.execute(
        """
        SELECT run_date, status FROM ticket_snapshots
        WHERE ticket_id = ? AND run_date >= ?
        ORDER BY run_date
        """,
        (ticket_id, since.isoformat()),
    ).fetchall()
    return [(date.fromisoformat(r["run_date"]), JiraStatus(r["status"])) for r in rows]
