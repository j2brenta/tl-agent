"""decisions repo — Phase 8 audit log."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from tl_agent.models import ApprovalAction, Decision, ResponseMode


def insert(conn: sqlite3.Connection, d: Decision) -> None:
    conn.execute(
        """
        INSERT INTO decisions (
            id, created_at, hotspot_id, proposed_mode, proposed_body,
            tl_action, tl_acted_at, final_body, final_target,
            trace_id, sent_message_id, sent_provider
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            tl_action = excluded.tl_action,
            tl_acted_at = excluded.tl_acted_at,
            final_body = excluded.final_body,
            final_target = excluded.final_target,
            sent_message_id = excluded.sent_message_id,
            sent_provider = excluded.sent_provider
        """,
        (
            d.id,
            d.created_at.isoformat(),
            d.hotspot_id,
            d.proposed_mode.value,
            d.proposed_body,
            d.tl_action.value if d.tl_action else None,
            d.tl_acted_at.isoformat() if d.tl_acted_at else None,
            d.final_body,
            d.final_target,
            d.trace_id,
            d.sent_message_id,
            d.sent_provider,
        ),
    )


def _row_to_decision(row: sqlite3.Row) -> Decision:
    return Decision(
        id=row["id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        hotspot_id=row["hotspot_id"],
        proposed_mode=ResponseMode(row["proposed_mode"]),
        proposed_body=row["proposed_body"],
        tl_action=ApprovalAction(row["tl_action"]) if row["tl_action"] else None,
        tl_acted_at=datetime.fromisoformat(row["tl_acted_at"]) if row["tl_acted_at"] else None,
        final_body=row["final_body"],
        final_target=row["final_target"],
        trace_id=row["trace_id"],
        sent_message_id=row["sent_message_id"],
        sent_provider=row["sent_provider"],
    )


def get(conn: sqlite3.Connection, decision_id: str) -> Decision | None:
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    return _row_to_decision(row) if row else None


def list_pending(conn: sqlite3.Connection) -> list[Decision]:
    """Drafts awaiting TL action — the Phase 8 review queue."""
    rows = conn.execute(
        "SELECT * FROM decisions WHERE tl_action IS NULL ORDER BY created_at ASC"
    ).fetchall()
    return [_row_to_decision(r) for r in rows]


def list_recent(conn: sqlite3.Connection, limit: int = 50) -> list[Decision]:
    rows = conn.execute(
        "SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_decision(r) for r in rows]
