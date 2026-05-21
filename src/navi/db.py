from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection that commits/rolls back and always closes."""
    with closing(sqlite3.connect(path)) as conn:
        with conn:
            yield conn
