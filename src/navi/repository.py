import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

from .db import connect, check_schema_version, write_schema_version

T = TypeVar("T")


class BaseRepository(Generic[T]):
    """Base DAO repository to eliminate boilerplate across domain stores."""

    _db_initialized: set[Path] = set()

    def __init__(
        self,
        db_path: Path,
        component: str,
        schema_version: int,
        ddl_statements: list[str],
        indices: list[str] | None = None,
        schema_validators: list[Callable[[sqlite3.Connection], None]] | None = None,
    ):
        self.db_path = db_path
        self.component = component
        self.schema_version = schema_version
        self.ddl_statements = ddl_statements
        self.indices = indices or []
        self.schema_validators = schema_validators or []
        if self.db_path not in self._db_initialized:
            self._init_db()
            BaseRepository._db_initialized.add(self.db_path)

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            check_schema_version(conn, self.component, self.schema_version)
            for ddl in self.ddl_statements:
                conn.execute(ddl)
            for validate in self.schema_validators:
                validate(conn)
            for idx in self.indices:
                conn.execute(idx)
            write_schema_version(conn, self.component, self.schema_version)

    def _json_dict(self, value: str | None) -> dict[str, Any]:
        """Unified JSON deserialization."""
        if not value:
            return {}
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def execute_read(self, query: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Execute a read query and return rows."""
        with connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(query, params).fetchall()

    def execute_write(self, query: str, params: tuple = ()) -> None:
        """Execute a write query."""
        with connect(self.db_path) as conn:
            conn.execute(query, params)
