"""engineer_baselines repo — rolling per-engineer metrics."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import NamedTuple


class Baseline(NamedTuple):
    """One per-engineer metric value over a rolling window."""

    engineer_id: str
    window: str  # "7d" / "30d"
    metric: str
    value: float
    computed_at: datetime


def upsert(conn: sqlite3.Connection, b: Baseline) -> None:
    conn.execute(
        """
        INSERT INTO engineer_baselines (engineer_id, window, metric, value, computed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(engineer_id, window, metric) DO UPDATE SET
            value = excluded.value,
            computed_at = excluded.computed_at
        """,
        (b.engineer_id, b.window, b.metric, b.value, b.computed_at.isoformat()),
    )


def get(
    conn: sqlite3.Connection,
    *,
    engineer_id: str,
    window: str,
    metric: str,
) -> Baseline | None:
    row = conn.execute(
        """
        SELECT * FROM engineer_baselines
        WHERE engineer_id = ? AND window = ? AND metric = ?
        """,
        (engineer_id, window, metric),
    ).fetchone()
    if not row:
        return None
    return Baseline(
        engineer_id=row["engineer_id"],
        window=row["window"],
        metric=row["metric"],
        value=float(row["value"]),
        computed_at=datetime.fromisoformat(row["computed_at"]),
    )


def list_for_engineer(conn: sqlite3.Connection, engineer_id: str) -> list[Baseline]:
    rows = conn.execute(
        "SELECT * FROM engineer_baselines WHERE engineer_id = ? ORDER BY window, metric",
        (engineer_id,),
    ).fetchall()
    return [
        Baseline(
            engineer_id=r["engineer_id"],
            window=r["window"],
            metric=r["metric"],
            value=float(r["value"]),
            computed_at=datetime.fromisoformat(r["computed_at"]),
        )
        for r in rows
    ]
