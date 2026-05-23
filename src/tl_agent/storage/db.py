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
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    """Apply schema.sql against the given connection (idempotent)."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)


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
