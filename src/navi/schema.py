"""Schema-as-code: single source of truth for SQLite table definitions.

Each table is declared once as a list of :class:`Column` values. From that
single declaration we derive:

* the ``CREATE TABLE IF NOT EXISTS`` DDL,
* the ordered ``SELECT`` column list,
* the schema-exact guard used to reject schema drift loudly.

This replaces the previous three-places-must-stay-in-sync pattern where each
store hand-wrote the ``CREATE TABLE`` SQL, a parallel ``_*_SCHEMA`` tuple list,
and a hand-typed ``SELECT`` column list.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Column:
    """A single SQLite column declaration.

    Attributes:
        name: Column name.
        sql_type: SQLite type affinity, uppercase: ``"TEXT"``, ``"REAL"``,
            ``"INTEGER"``.
        nullable: When ``False`` the column is declared ``NOT NULL``.
        primary_key: When ``True`` the column is declared ``PRIMARY KEY``.
        unique: When ``True`` the column is declared ``UNIQUE``.
        default: Raw SQL default expression (e.g. ``"''"``, ``"'recurring'"``)
            or ``None`` for no default.
    """

    name: str
    sql_type: str
    nullable: bool = True
    primary_key: bool = False
    unique: bool = False
    default: str | None = None


_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class Table:
    """A named table backed by a column list."""

    name: str
    columns: list[Column]

    def __post_init__(self) -> None:
        # Defense in depth: SQL identifiers cannot be parameterized, so table
        # names are interpolated into PRAGMA/DROP/CREATE DDL via f-strings. They
        # are schema-as-code (code-controlled) today; validating the identifier
        # shape at construction guarantees no future Table can smuggle SQL
        # through ``name``, making every ``f"...{table.name}"`` site safe.
        if not _TABLE_NAME_RE.match(self.name):
            raise ValueError(f"invalid SQL table identifier: {self.name!r}")

    @property
    def ddl(self) -> str:
        """Full ``CREATE TABLE IF NOT EXISTS`` statement for this table."""
        col_defs = ",\n    ".join(_column_ddl(col) for col in self.columns)
        return f"CREATE TABLE IF NOT EXISTS {self.name} (\n    {col_defs}\n)"

    @property
    def select_list(self) -> str:
        """Comma-separated, ordered column list for ``SELECT`` / ``INSERT``."""
        return ", ".join(col.name for col in self.columns)

    @property
    def pragma_tuples(self) -> list[tuple[str, str, int, int]]:
        """The ``(name, type, notnull, pk)`` tuples ``PRAGMA table_info``
        yields for each column — the shape :func:`assert_schema_exact`
        compares against."""
        return [_pragma_tuple(col) for col in self.columns]


def _column_ddl(col: Column) -> str:
    """Render a single column's DDL clause.

    Constraint ordering matches the historical hand-written DDL so the
    on-disk schema is unchanged: PRIMARY KEY, NOT NULL, UNIQUE, DEFAULT.
    """
    parts: list[str] = [col.name, col.sql_type]
    if col.primary_key:
        parts.append("PRIMARY KEY")
    if not col.nullable:
        parts.append("NOT NULL")
    if col.unique:
        parts.append("UNIQUE")
    if col.default is not None:
        parts.append(f"DEFAULT {col.default}")
    return " ".join(parts)


def _pragma_tuple(col: Column) -> tuple[str, str, int, int]:
    """The 4-tuple ``(name, type, notnull, pk)`` that ``PRAGMA table_info``
    yields for this column, used by :func:`assert_schema_exact`."""
    return (
        col.name,
        col.sql_type.upper(),
        0 if col.nullable else 1,
        1 if col.primary_key else 0,
    )


def assert_schema_exact(
    conn: sqlite3.Connection, table: Table
) -> None:
    """Reject schema drift loudly.

    Compares the live ``PRAGMA table_info`` shape against the declared
    :class:`Table` columns. Any mismatch (missing column, renamed column,
    type change, nullability change) raises ``RuntimeError`` rather than
    silently adapting — so a stale on-disk schema is caught at startup.
    """
    expected = [_pragma_tuple(col) for col in table.columns]
    actual = [
        (row[1], str(row[2]).upper(), int(row[3]), int(row[5]))
        for row in conn.execute(f"PRAGMA table_info({table.name})").fetchall()
    ]
    if actual != expected:
        raise RuntimeError(f"{table.name} schema mismatch; expected current Navi schema")
