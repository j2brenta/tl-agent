"""SQLite-backed idempotency store + dedup key helper.

This is the writer-safety net. Every writer tool declares an `idempotency_key`
derived from (tool_name, normalized_args, run_date). The first call writes the
key + result; any subsequent call with the same key inside the TTL window
returns the cached result without invoking the upstream.

TTL default: 24 hours. Per-run scope by including `run_date` in the key —
a re-run on a new day will deliberately not dedup.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from tl_agent.tools.base import IdempotencyLookup, normalize_args, stable_hash

DEFAULT_TTL = timedelta(hours=24)


def make_key(*, tool_name: str, args: BaseModel, run_date_iso: str) -> str:
    """Stable hash for an idempotency key.

    Writers should call this rather than rolling their own — guarantees the
    same normalization the store will look up against.
    """
    return stable_hash([tool_name, run_date_iso, normalize_args(args)])


class SqliteIdempotencyStore(IdempotencyLookup):
    """SQLite-backed implementation of `IdempotencyLookup`.

    Operations run synchronously inside the async wrapper — SQLite calls on
    local files (or :memory:) are sub-millisecond and don't justify the
    cross-thread complexity of `asyncio.to_thread`. The orchestrator owns one
    connection per run; tools don't share it across threads.
    """

    def __init__(self, conn: sqlite3.Connection, ttl: timedelta = DEFAULT_TTL) -> None:
        self._conn = conn
        self._ttl = ttl

    async def get(self, key: str) -> object | None:
        cutoff = (datetime.now(UTC) - self._ttl).isoformat()
        row = self._conn.execute(
            """
            SELECT result_json FROM idempotency_keys
            WHERE key = ? AND created_at >= ?
            """,
            (key, cutoff),
        ).fetchone()
        if not row:
            return None
        decoded: object = json.loads(row["result_json"])
        return decoded

    async def put(
        self,
        *,
        key: str,
        tool_name: str,
        run_date_iso: str,
        result_json: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO idempotency_keys
                (key, tool_name, run_date, created_at, result_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (key, tool_name, run_date_iso, datetime.now(UTC).isoformat(), result_json),
        )

    async def evict_older_than(self, older_than: timedelta) -> int:
        """Maintenance: drop keys older than `older_than`. Returns count deleted."""
        cutoff = (datetime.now(UTC) - older_than).isoformat()
        cur = self._conn.execute(
            "DELETE FROM idempotency_keys WHERE created_at < ?",
            (cutoff,),
        )
        return cur.rowcount
