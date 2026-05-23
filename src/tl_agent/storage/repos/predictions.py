"""predictions repo — falsifiable claims, closed out by Phase 0."""

from __future__ import annotations

import sqlite3
from datetime import date

from tl_agent.models import Prediction, PredictionOutcome


def insert(conn: sqlite3.Connection, p: Prediction) -> None:
    conn.execute(
        """
        INSERT INTO predictions (
            id, made_on, claim, related_hotspot_id, resolve_after, outcome,
            resolved_on, resolution_note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            outcome = excluded.outcome,
            resolved_on = excluded.resolved_on,
            resolution_note = excluded.resolution_note
        """,
        (
            p.id,
            p.made_on.isoformat(),
            p.claim,
            p.related_hotspot_id,
            p.resolve_after.isoformat(),
            p.outcome.value,
            p.resolved_on.isoformat() if p.resolved_on else None,
            p.resolution_note,
        ),
    )


def _row_to_prediction(row: sqlite3.Row) -> Prediction:
    return Prediction(
        id=row["id"],
        made_on=date.fromisoformat(row["made_on"]),
        claim=row["claim"],
        related_hotspot_id=row["related_hotspot_id"],
        resolve_after=date.fromisoformat(row["resolve_after"]),
        outcome=PredictionOutcome(row["outcome"]),
        resolved_on=date.fromisoformat(row["resolved_on"]) if row["resolved_on"] else None,
        resolution_note=row["resolution_note"],
    )


def list_due(conn: sqlite3.Connection, today: date) -> list[Prediction]:
    """Predictions whose resolve_after has passed and are still open."""
    rows = conn.execute(
        """
        SELECT * FROM predictions
        WHERE outcome = 'open' AND resolve_after <= ?
        ORDER BY resolve_after ASC
        """,
        (today.isoformat(),),
    ).fetchall()
    return [_row_to_prediction(r) for r in rows]


def list_made_on(conn: sqlite3.Connection, made_on: date) -> list[Prediction]:
    rows = conn.execute(
        "SELECT * FROM predictions WHERE made_on = ? ORDER BY id",
        (made_on.isoformat(),),
    ).fetchall()
    return [_row_to_prediction(r) for r in rows]
