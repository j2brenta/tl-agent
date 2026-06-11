"""resolved_config repo — runtime-resolved values that aren't authored in LAYER 1.

Currently holds the Jira board discovered when `config/team.md` omits
`board_id`. `config/team.md` stays the authoritative override; this is the
learned cache so discovery (and the human "which board?" gate) happens once,
not every run.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

# Key for the discovered/chosen Jira agile board id.
JIRA_BOARD_KEY = "jira_board_id"


def get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM resolved_config WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row is not None else None


def set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO resolved_config (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, datetime.now(UTC).isoformat()),
    )
    conn.commit()
