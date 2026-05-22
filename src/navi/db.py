from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection that commits/rolls back and always closes."""
    with closing(sqlite3.connect(path, timeout=30.0)) as conn:
        _execute_with_busy_retry(conn, "PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        with conn:
            yield conn


def _execute_with_busy_retry(conn: sqlite3.Connection, sql: str) -> None:
    for attempt in range(6):
        try:
            conn.execute(sql)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))
