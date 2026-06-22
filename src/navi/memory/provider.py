"""Memory persistence: MemoryProvider protocol + SQLiteMemoryProvider."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from ..db import connect
from .models import MemoryItem, SessionAlias, StoredMessage


class MemoryProvider(Protocol):
    def store_item(self, item: MemoryItem) -> None: ...
    def get_items(
        self, *, memory_type: str | None = None, status: str | None = None, limit: int = 50
    ) -> list[MemoryItem]: ...
    def get_item(self, item_id: str) -> MemoryItem | None: ...
    def update_item(
        self,
        item_id: str,
        *,
        status: str | None = None,
        last_verified_at: float | None = None,
        updated_at: float | None = None,
    ) -> None: ...
    def delete_item(self, item_id: str) -> None: ...
    def add_message(self, session_id: str, role: str, content: str, created_at: float) -> None: ...
    def get_messages(self, session_id: str, limit: int = 50) -> list[StoredMessage]: ...
    def list_sessions(self) -> list[str]: ...
    def set_session_alias(
        self, alias: str, session_id: str, created_at: float, updated_at: float
    ) -> None: ...
    def get_session_alias(self, alias: str) -> SessionAlias | None: ...
    def list_session_aliases(self, limit: int = 50) -> list[SessionAlias]: ...


class SQLiteMemoryProvider:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_aliases (
                    alias TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_items (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_verified_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    metadata TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_status ON memory_items(status, type)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_items(scope)")

            cursor = conn.execute("PRAGMA table_info(memory_items)")
            columns = [row[1] for row in cursor.fetchall()]
            if "reason" not in columns:
                conn.execute("ALTER TABLE memory_items ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
            if "provenance" not in columns:
                conn.execute(
                    "ALTER TABLE memory_items ADD COLUMN provenance TEXT NOT NULL DEFAULT ''"
                )

    def _item_from_row(self, row: tuple) -> MemoryItem:
        return MemoryItem(
            id=row[0],
            type=row[1],
            status=row[2],
            scope=row[3],
            content=row[4],
            source=row[5],
            confidence=row[6],
            created_at=row[7],
            updated_at=row[8],
            last_verified_at=row[9],
            expires_at=row[10],
            metadata=json.loads(row[11]),
            reason=row[12],
            provenance=row[13],
        )

    def store_item(self, item: MemoryItem) -> None:
        if not item.source.strip():
            raise ValueError("memory source is required")
        if not item.reason.strip():
            raise ValueError("memory reason is required")
        if not item.provenance.strip():
            raise ValueError("memory provenance is required")
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO memory_items(
                    id, type, status, scope, content, source, confidence,
                    created_at, updated_at, last_verified_at, expires_at, metadata,
                    reason, provenance
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.type,
                    item.status,
                    item.scope,
                    item.content,
                    item.source,
                    item.confidence,
                    item.created_at,
                    item.updated_at,
                    item.last_verified_at,
                    item.expires_at,
                    json.dumps(item.metadata, sort_keys=True),
                    item.reason,
                    item.provenance,
                ),
            )

    def get_items(
        self, *, memory_type: str | None = None, status: str | None = None, limit: int = 50
    ) -> list[MemoryItem]:
        clauses = []
        values: list[object] = []
        if memory_type:
            clauses.append("type = ?")
            values.append(memory_type)
        if status:
            clauses.append("status = ?")
            values.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT id, type, status, scope, content, source, confidence,
                       created_at, updated_at, last_verified_at, expires_at, metadata,
                       reason, provenance
                FROM memory_items
                {where}
                ORDER BY updated_at DESC LIMIT ?
                """,
                values,
            ).fetchall()
        return [self._item_from_row(row) for row in rows]

    def get_item(self, item_id: str) -> MemoryItem | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, type, status, scope, content, source, confidence,
                       created_at, updated_at, last_verified_at, expires_at, metadata,
                       reason, provenance
                FROM memory_items WHERE id = ?
                """,
                (item_id,),
            ).fetchone()
        return self._item_from_row(row) if row else None

    def update_item(
        self,
        item_id: str,
        *,
        status: str | None = None,
        last_verified_at: float | None = None,
        updated_at: float | None = None,
    ) -> None:
        sets = []
        values: list[object] = []
        if status is not None:
            sets.append("status = ?")
            values.append(status)
        if last_verified_at is not None:
            sets.append("last_verified_at = ?")
            values.append(last_verified_at)
        if updated_at is not None:
            sets.append("updated_at = ?")
            values.append(updated_at)
        if not sets:
            return
        values.append(item_id)
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE memory_items SET " + ", ".join(sets) + " WHERE id = ?",
                values,
            )

    def delete_item(self, item_id: str) -> None:
        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM memory_items WHERE id = ?", (item_id,))

    def add_message(self, session_id: str, role: str, content: str, created_at: float) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO messages(session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_id, role, content, created_at),
            )

    def get_messages(self, session_id: str, limit: int = 50) -> list[StoredMessage]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT session_id, role, content, created_at
                FROM messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [StoredMessage(*row) for row in reversed(rows)]

    def list_sessions(self) -> list[str]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT session_id FROM messages GROUP BY session_id ORDER BY MAX(created_at) DESC"
            ).fetchall()
        return [row[0] for row in rows]

    def set_session_alias(
        self, alias: str, session_id: str, created_at: float, updated_at: float
    ) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO session_aliases(alias, session_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(alias) DO UPDATE SET session_id = excluded.session_id, updated_at = excluded.updated_at
                """,
                (alias, session_id, created_at, updated_at),
            )

    def get_session_alias(self, alias: str) -> SessionAlias | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT alias, session_id, created_at, updated_at
                FROM session_aliases WHERE alias = ?
                """,
                (alias,),
            ).fetchone()
        return SessionAlias(*row) if row else None

    def list_session_aliases(self, limit: int = 50) -> list[SessionAlias]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT alias, session_id, created_at, updated_at
                FROM session_aliases ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [SessionAlias(*row) for row in rows]
