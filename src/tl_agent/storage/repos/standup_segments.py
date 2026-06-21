"""standup_segments repo — cached per-message standup segmentation.

`(chat_message_id, engineer_id, segment_index)` is the cache key: a message
that's already been segmented and classified (`update` vs `off_topic`) is
never re-sent to the LLM, whether the parse was triggered by the Workflow
"Collect Standup" button, the Sprint page's "Import from Mattermost", or a
pipeline run.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from tl_agent.models.signals import StandupSegment, StandupSegmentKind


def upsert_many(conn: sqlite3.Connection, segments: Iterable[StandupSegment]) -> None:
    for seg in segments:
        conn.execute(
            """
            INSERT INTO standup_segments (
                id, chat_message_id, chat_channel_id, engineer_id, date_iso,
                segment_index, text, kind
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_message_id, engineer_id, segment_index) DO UPDATE SET
                chat_channel_id = excluded.chat_channel_id,
                date_iso        = excluded.date_iso,
                text            = excluded.text,
                kind            = excluded.kind
            """,
            (
                f"{seg.chat_message_id}:{seg.engineer_id}:{seg.segment_index}",
                seg.chat_message_id,
                seg.chat_channel_id,
                seg.engineer_id,
                seg.date_iso,
                seg.segment_index,
                seg.text,
                seg.kind.value,
            ),
        )


def get_for_message(
    conn: sqlite3.Connection, *, chat_message_id: str, engineer_id: str
) -> list[StandupSegment]:
    """Existing segments for one message, ordered by `segment_index`.

    An empty list means the message hasn't been parsed yet.
    """
    rows = conn.execute(
        """
        SELECT * FROM standup_segments
        WHERE chat_message_id = ? AND engineer_id = ?
        ORDER BY segment_index
        """,
        (chat_message_id, engineer_id),
    ).fetchall()
    return [_row_to_segment(r) for r in rows]


def delete_for_message(conn: sqlite3.Connection, *, chat_message_id: str, engineer_id: str) -> None:
    """Drop a message's cached segments so the next parse re-runs.

    The manual-entry path reuses a deterministic `chat_message_id`
    (`manual:{date}:{engineer_id}`); deleting before re-parse means an edited
    resubmission re-segments instead of returning the stale cached result.
    """
    conn.execute(
        "DELETE FROM standup_segments WHERE chat_message_id = ? AND engineer_id = ?",
        (chat_message_id, engineer_id),
    )


def list_for_engineer_date(
    conn: sqlite3.Connection, *, engineer_id: str, date_iso: str
) -> list[StandupSegment]:
    """All segments for one engineer on one day, ordered for display."""
    rows = conn.execute(
        """
        SELECT * FROM standup_segments
        WHERE engineer_id = ? AND date_iso = ?
        ORDER BY chat_message_id, segment_index
        """,
        (engineer_id, date_iso),
    ).fetchall()
    return [_row_to_segment(r) for r in rows]


def list_for_date(conn: sqlite3.Connection, date_iso: str) -> list[StandupSegment]:
    """All segments classified on one day, ordered for display."""
    rows = conn.execute(
        """
        SELECT * FROM standup_segments
        WHERE date_iso = ?
        ORDER BY engineer_id, chat_message_id, segment_index
        """,
        (date_iso,),
    ).fetchall()
    return [_row_to_segment(r) for r in rows]


def _row_to_segment(row: sqlite3.Row) -> StandupSegment:
    return StandupSegment(
        engineer_id=row["engineer_id"],
        date_iso=row["date_iso"],
        chat_message_id=row["chat_message_id"],
        chat_channel_id=row["chat_channel_id"],
        segment_index=row["segment_index"],
        text=row["text"],
        kind=StandupSegmentKind(row["kind"]),
    )
