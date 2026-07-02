"""Memory persistence: MemoryProvider protocol + SQLiteMemoryProvider."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol

from ..db import connect, ensure_schema_version
from ..schema import Column, Table, assert_schema_exact
from .models import MemoryItem, SessionAlias, StoredMessage

logger = logging.getLogger("navi.memory.provider")

MEMORY_SCHEMA_VERSION = 1

MESSAGES_TABLE = Table(
    "messages",
    [
        Column("id", "INTEGER", primary_key=True),
        Column("session_id", "TEXT", nullable=False),
        Column("role", "TEXT", nullable=False),
        Column("content", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
    ],
)
SESSION_ALIASES_TABLE = Table(
    "session_aliases",
    [
        Column("alias", "TEXT", primary_key=True),
        Column("session_id", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
        Column("updated_at", "REAL", nullable=False),
    ],
)
MEMORY_ITEMS_TABLE = Table(
    "memory_items",
    [
        Column("id", "TEXT", primary_key=True),
        Column("type", "TEXT", nullable=False),
        Column("status", "TEXT", nullable=False),
        Column("scope", "TEXT", nullable=False),
        Column("content", "TEXT", nullable=False),
        Column("source", "TEXT", nullable=False),
        Column("confidence", "REAL", nullable=False),
        Column("created_at", "REAL", nullable=False),
        Column("updated_at", "REAL", nullable=False),
        Column("last_verified_at", "REAL", nullable=False),
        Column("expires_at", "REAL", nullable=False),
        Column("metadata", "TEXT", nullable=False),
        Column("reason", "TEXT", nullable=False, default="''"),
        Column("provenance", "TEXT", nullable=False, default="''"),
    ],
)


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
        confidence: float | None = None,
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
            ensure_schema_version(conn, "memory", MEMORY_SCHEMA_VERSION)
            conn.execute(MESSAGES_TABLE.ddl)
            assert_schema_exact(conn, MESSAGES_TABLE)
            conn.execute(SESSION_ALIASES_TABLE.ddl)
            assert_schema_exact(conn, SESSION_ALIASES_TABLE)
            conn.execute(MEMORY_ITEMS_TABLE.ddl)
            self._migrate_memory_items(conn)
            assert_schema_exact(conn, MEMORY_ITEMS_TABLE)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_status ON memory_items(status, type)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_items(scope)")
            
            # FTS5 trigram table for fast keyword search
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
                "id UNINDEXED, content, tokenize='trigram'"
                ")"
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS memory_items_ai AFTER INSERT ON memory_items
                BEGIN
                    DELETE FROM memory_fts WHERE id = new.id;
                    INSERT INTO memory_fts(id, content) VALUES (new.id, new.content);
                END;
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS memory_items_ad AFTER DELETE ON memory_items
                BEGIN
                    DELETE FROM memory_fts WHERE id = old.id;
                END;
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS memory_items_au AFTER UPDATE ON memory_items
                BEGIN
                    DELETE FROM memory_fts WHERE id = old.id;
                    INSERT INTO memory_fts(id, content) VALUES (new.id, new.content);
                END;
                """
            )
            # Backfill any existing items that aren't in the FTS table
            conn.execute(
                "INSERT INTO memory_fts(id, content) "
                "SELECT id, content FROM memory_items "
                "WHERE id NOT IN (SELECT id FROM memory_fts)"
            )

    @staticmethod
    def _migrate_memory_items(conn) -> None:
        """Backfill reason/provenance on pre-schema-version memory.db.

        Principle 1.2: the schema-exact guard rejects drift loudly, so the
        on-disk shape must reach the current contract before the assertion
        runs. reason/provenance were added after the initial memory_items
        table; ALTER them in if missing."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_items)")}
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

    def store_item_with_contradictions(self, item: MemoryItem) -> MemoryItem:
        """Store ``item`` and recompute its ``contradicts`` set against the
        currently-active items of the same type — all within a single
        transaction so a concurrent writer cannot interleave between the read
        and the write (principle 1.2/16). Returns the stored item with its
        updated metadata."""
        import difflib

        if not item.source.strip() or not item.reason.strip() or not item.provenance.strip():
            raise ValueError("memory source, reason, and provenance are required")
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, type, status, scope, content, source, confidence,
                       created_at, updated_at, last_verified_at, expires_at, metadata,
                       reason, provenance
                FROM memory_items
                WHERE type = ? AND status = 'active'
                """,
                (item.type,),
            ).fetchall()
            metadata = dict(item.metadata)
            contradicts = set(metadata.get("contradicts", []))
            for row in rows:
                existing = self._item_from_row(row)
                if existing.id == item.id:
                    continue
                if existing.scope == item.scope:
                    ratio = difflib.SequenceMatcher(
                        None, existing.content.lower(), item.content.lower()
                    ).ratio()
                    if ratio > 0.85 and existing.content.lower() != item.content.lower():
                        contradicts.add(existing.id)
            if contradicts:
                metadata["contradicts"] = sorted(contradicts)
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
                    json.dumps(metadata, sort_keys=True),
                    item.reason,
                    item.provenance,
                ),
            )
        return MemoryItem(
            id=item.id,
            type=item.type,
            status=item.status,
            scope=item.scope,
            content=item.content,
            source=item.source,
            confidence=item.confidence,
            created_at=item.created_at,
            updated_at=item.updated_at,
            last_verified_at=item.last_verified_at,
            expires_at=item.expires_at,
            metadata=metadata,
            reason=item.reason,
            provenance=item.provenance,
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
        confidence: float | None = None,
    ) -> None:
        sets = []
        values: list[object] = []
        if status is not None:
            sets.append("status = ?")
            values.append(status)
        if last_verified_at is not None:
            sets.append("last_verified_at = ?")
            values.append(last_verified_at)
        if confidence is not None:
            sets.append("confidence = ?")
            values.append(confidence)
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

    def search_fts(self, query: str, limit: int) -> list[tuple[str, float]]:
        """Query the FTS5 trigram table and return a list of (item_id, rank)."""
        # Escape double quotes to avoid FTS syntax errors if query has them
        safe_query = query.replace('"', '""')
        match_expr = f'"{safe_query}"'
        try:
            with connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT id, rank "
                    "FROM memory_fts "
                    "WHERE memory_fts MATCH ? "
                    "ORDER BY rank "
                    "LIMIT ?",
                    (match_expr, limit),
                ).fetchall()
                return [(row[0], float(row[1])) for row in rows]
        except Exception:
            logger.debug("search_fts failed for query %r", query, exc_info=True)
            return []



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
