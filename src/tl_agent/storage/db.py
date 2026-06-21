"""SQLite connection + schema initialization.

We use a thin wrapper instead of an ORM because:
- The schema is small (8 tables), explicit SQL is easier to audit.
- FTS5 triggers don't fit cleanly into ORM models.
- Repos can stay typed at the boundary (Pydantic) without ORM-mapper overhead.

Connection management: one connection per process for sync code (orchestrator),
opened with `connect()`. Tests pass `path=":memory:"` for isolation. WAL +
NORMAL sync + FK ON enabled at connect time.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from tl_agent.settings import get_settings

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with the conventions we expect.

    Args:
        path: filesystem path or ":memory:" for tests. Defaults to settings.
    """
    target = str(path) if path is not None else str(get_settings().sqlite_path)
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        target,
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,  # autocommit; transactions managed by `transaction()`
    )
    conn.row_factory = sqlite3.Row
    # busy_timeout first so the lock-taking statements below get the 5s wait.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL by default; the container uses TRUNCATE because WAL's mmap'd `-shm`
    # file is unreliable on a Docker bind mount (see Settings.sqlite_journal_mode).
    journal_mode = "WAL" if target == ":memory:" else get_settings().sqlite_journal_mode
    # Changing journal mode takes a brief EXCLUSIVE lock, and SQLite does NOT
    # invoke the busy handler for it — against a concurrent writer (a background
    # run's per-phase checkpoints) it returns "database is locked" immediately.
    # The mode is an optimization, not a correctness requirement: the default
    # rollback journal (DELETE) is just as bind-mount-safe as TRUNCATE, and a
    # DB already in WAL no-ops this. So make it best-effort rather than 500 the
    # request that happened to connect mid-write.
    try:
        conn.execute(f"PRAGMA journal_mode = {journal_mode}")
    except sqlite3.OperationalError:
        logger.warning("could not set journal_mode=%s (db busy); using default", journal_mode)
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    """Apply schema.sql against the given connection (idempotent).

    SQLite's `CREATE TABLE IF NOT EXISTS` doesn't reconcile columns when a
    table exists with an older shape. Before running the schema we apply
    forward-only ALTERs for known additive migrations so the rest of
    schema.sql (indexes etc.) can reference the new columns.
    """
    _apply_lightweight_migrations(conn)
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)


_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # (table, column, "type + default" suffix used in ALTER TABLE)
    ("decisions", "run_date", "TEXT NOT NULL DEFAULT ''"),
    ("decisions", "needs_review", "INTEGER NOT NULL DEFAULT 0"),
    ("collection_state", "standup_collected_at", "TEXT"),
    ("collection_state", "standups_count", "INTEGER"),
)


def _apply_lightweight_migrations(conn: sqlite3.Connection) -> None:
    """Add missing columns on existing tables. Idempotent + cheap."""
    existing_tables: set[str] = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for table, column, type_default in _ADDITIVE_COLUMNS:
        if table not in existing_tables:
            continue  # CREATE TABLE in schema.sql will produce the right shape
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_default}")
    # Backfill run_date on old decisions rows from their created_at prefix
    # so they don't silently land under "" in the filter.
    if "decisions" in existing_tables:
        conn.execute(
            "UPDATE decisions SET run_date = substr(created_at, 1, 10) "
            "WHERE run_date = '' OR run_date IS NULL"
        )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[None]:
    """Explicit transaction since autocommit is on at connection level."""
    conn.execute("BEGIN")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def init_db_cli() -> None:
    """`python -m tl_agent.storage.db --init` — create the DB if missing."""
    parser = argparse.ArgumentParser(description="Initialise tl-agent SQLite DB")
    parser.add_argument("--init", action="store_true", help="apply schema.sql")
    parser.add_argument("--path", default=None, help="override DB path")
    args = parser.parse_args()
    if args.init:
        conn = connect(args.path)
        initialize(conn)
        conn.close()
        logger.info("initialised SQLite at %s", args.path or get_settings().sqlite_path)


if __name__ == "__main__":
    init_db_cli()
