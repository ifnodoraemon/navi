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


def check_schema_version(conn: sqlite3.Connection, component: str, version: int) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_versions (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    row = conn.execute(
        "SELECT version FROM schema_versions WHERE component = ?", (component,)
    ).fetchone()
    if row is not None and int(row[0]) > version:
        raise RuntimeError(
            f"{component} schema version {row[0]} is newer than supported version {version}"
        )


def write_schema_version(conn: sqlite3.Connection, component: str, version: int) -> None:
    conn.execute(
        """
        INSERT INTO schema_versions(component, version, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(component) DO UPDATE SET version = excluded.version, updated_at = excluded.updated_at
        """,
        (component, version, time.time()),
    )
