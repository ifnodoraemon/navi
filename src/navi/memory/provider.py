"""Memory persistence: MemoryProvider protocol + SQLiteMemoryProvider."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol

from ..db import connect, check_schema_version, write_schema_version
from ..schema import Column, Table, assert_schema_exact
from .models import MemoryItem, SessionAlias, StoredMessage

logger = logging.getLogger("navi.memory.provider")

MEMORY_SCHEMA_VERSION = 2

MESSAGES_TABLE = Table(
    "messages",
    [
        Column("id", "INTEGER", primary_key=True),
        Column("session_id", "TEXT", nullable=False),
        Column("role", "TEXT", nullable=False),
        Column("content", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
        Column("message_id", "TEXT", nullable=False, default="''"),
        Column("source", "TEXT", nullable=False, default="''"),
        Column("peer_id", "TEXT", nullable=False, default="''"),
        Column("sender_id", "TEXT", nullable=False, default="''"),
        Column("trace_id", "TEXT", nullable=False, default="''"),
        Column("run_id", "TEXT", nullable=False, default="''"),
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
    def store_item_with_contradictions(self, item: MemoryItem) -> MemoryItem: ...
    def get_items(
        self,
        *,
        memory_type: str | None = None,
        status: str | None = None,
        allowed_scopes: set[str] | frozenset[str] | None = None,
        limit: int = 50,
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
    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        created_at: float,
        *,
        message_id: str = "",
        source: str = "",
        peer_id: str = "",
        sender_id: str = "",
        trace_id: str = "",
        run_id: str = "",
    ) -> None: ...
    def get_messages(self, session_id: str, limit: int = 50) -> list[StoredMessage]: ...
    def get_messages_for_run(
        self, session_id: str, run_id: str, limit: int = 50
    ) -> list[StoredMessage]: ...
    def search_messages_fts(
        self,
        query: str,
        limit: int,
        *,
        session_id: str = "",
        source: str = "",
        peer_id: str = "",
        sender_id: str = "",
    ) -> list[tuple[StoredMessage, float, list[str]]]: ...
    def clear_messages(self, session_id: str) -> int: ...
    def list_sessions(self) -> list[str]: ...
    def set_session_alias(
        self, alias: str, session_id: str, created_at: float, updated_at: float
    ) -> None: ...
    def get_session_alias(self, alias: str) -> SessionAlias | None: ...
    def list_session_aliases(self, limit: int = 50) -> list[SessionAlias]: ...
    def search_fts(
        self,
        query: str,
        limit: int,
        *,
        allowed_scopes: set[str] | frozenset[str] | None = None,
    ) -> list[tuple[str, float]]: ...


class SQLiteMemoryProvider:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            check_schema_version(conn, "memory", MEMORY_SCHEMA_VERSION)
            conn.execute(MESSAGES_TABLE.ddl)
            _migrate_messages_table(conn)
            assert_schema_exact(conn, MESSAGES_TABLE)
            conn.execute(SESSION_ALIASES_TABLE.ddl)
            assert_schema_exact(conn, SESSION_ALIASES_TABLE)
            conn.execute(MEMORY_ITEMS_TABLE.ddl)
            assert_schema_exact(conn, MEMORY_ITEMS_TABLE)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_consolidation_jobs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    peer_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    attempts INTEGER NOT NULL,
                    error TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(session_id, run_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_jobs_pending "
                "ON memory_consolidation_jobs(status, lease_expires_at, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_identity "
                "ON messages(source, peer_id, sender_id, id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_status ON memory_items(status, type)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_items(scope)")

            # FTS5 trigram table for deterministic conversation-message search.
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
                "message_id UNINDEXED, content, tokenize='trigram'"
                ")"
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages
                BEGIN
                    DELETE FROM messages_fts WHERE message_id = new.message_id;
                    INSERT INTO messages_fts(message_id, content)
                    VALUES (new.message_id, new.content);
                END;
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages
                BEGIN
                    DELETE FROM messages_fts WHERE message_id = old.message_id;
                END;
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages
                BEGIN
                    DELETE FROM messages_fts WHERE message_id = old.message_id;
                    INSERT INTO messages_fts(message_id, content)
                    VALUES (new.message_id, new.content);
                END;
                """
            )
            conn.execute(
                "INSERT INTO messages_fts(message_id, content) "
                "SELECT message_id, content FROM messages "
                "WHERE message_id NOT IN (SELECT message_id FROM messages_fts)"
            )

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
            write_schema_version(conn, "memory", MEMORY_SCHEMA_VERSION)

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

    def _message_from_row(self, row: tuple) -> StoredMessage:
        return StoredMessage(
            session_id=row[0],
            role=row[1],
            content=row[2],
            created_at=row[3],
            message_id=row[4],
            source=row[5],
            peer_id=row[6],
            sender_id=row[7],
            trace_id=row[8],
            run_id=row[9],
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
        self,
        *,
        memory_type: str | None = None,
        status: str | None = None,
        allowed_scopes: set[str] | frozenset[str] | None = None,
        limit: int = 50,
    ) -> list[MemoryItem]:
        clauses = []
        values: list[object] = []
        if memory_type:
            clauses.append("type = ?")
            values.append(memory_type)
        if status:
            clauses.append("status = ?")
            values.append(status)
        if allowed_scopes is not None:
            scopes = sorted(allowed_scopes)
            if not scopes:
                return []
            clauses.append("scope IN (" + ",".join("?" for _ in scopes) + ")")
            values.extend(scopes)
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

    def search_fts(
        self,
        query: str,
        limit: int,
        *,
        allowed_scopes: set[str] | frozenset[str] | None = None,
    ) -> list[tuple[str, float]]:
        """Query the FTS5 trigram table and return a list of (item_id, rank)."""
        # Escape double quotes to avoid FTS syntax errors if query has them
        safe_query = query.replace('"', '""')
        match_expr = f'"{safe_query}"'
        try:
            with connect(self.db_path) as conn:
                if allowed_scopes is None:
                    rows = conn.execute(
                        "SELECT id, rank FROM memory_fts "
                        "WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?",
                        (match_expr, limit),
                    ).fetchall()
                else:
                    scopes = sorted(allowed_scopes)
                    if not scopes:
                        return []
                    placeholders = ",".join("?" for _ in scopes)
                    rows = conn.execute(
                        "SELECT memory_fts.id, memory_fts.rank "
                        "FROM memory_fts JOIN memory_items "
                        "ON memory_items.id = memory_fts.id "
                        "WHERE memory_fts MATCH ? "
                        f"AND memory_items.scope IN ({placeholders}) "
                        "ORDER BY memory_fts.rank LIMIT ?",
                        (match_expr, *scopes, limit),
                    ).fetchall()
                return [(row[0], float(row[1])) for row in rows]
        except Exception:
            logger.debug("search_fts failed for query %r", query, exc_info=True)
            return []



    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        created_at: float,
        *,
        message_id: str = "",
        source: str = "",
        peer_id: str = "",
        sender_id: str = "",
        trace_id: str = "",
        run_id: str = "",
    ) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO messages(
                    session_id, role, content, created_at, message_id,
                    source, peer_id, sender_id, trace_id, run_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    role,
                    content,
                    created_at,
                    message_id,
                    source,
                    peer_id,
                    sender_id,
                    trace_id,
                    run_id,
                ),
            )

    def get_messages(self, session_id: str, limit: int = 50) -> list[StoredMessage]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT session_id, role, content, created_at, message_id,
                       source, peer_id, sender_id, trace_id, run_id
                FROM messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [self._message_from_row(row) for row in reversed(rows)]

    def get_messages_for_run(
        self,
        session_id: str,
        run_id: str,
        limit: int = 50,
    ) -> list[StoredMessage]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT session_id, role, content, created_at, message_id,
                       source, peer_id, sender_id, trace_id, run_id
                FROM messages
                WHERE session_id = ? AND run_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, run_id, limit),
            ).fetchall()
        return [self._message_from_row(row) for row in reversed(rows)]

    def search_messages_fts(
        self,
        query: str,
        limit: int,
        *,
        session_id: str = "",
        source: str = "",
        peer_id: str = "",
        sender_id: str = "",
    ) -> list[tuple[StoredMessage, float, list[str]]]:
        query = query.strip()
        if not query:
            return []
        where_sql, where_values = _message_identity_filter(
            session_id=session_id,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
        )
        if not where_sql:
            return []
        safe_query = query.replace('"', '""')
        match_expr = f'"{safe_query}"'
        rows: list[tuple] = []
        try:
            with connect(self.db_path) as conn:
                rows = conn.execute(
                    f"""
                    SELECT messages.session_id, messages.role, messages.content,
                           messages.created_at, messages.message_id, messages.source,
                           messages.peer_id, messages.sender_id, messages.trace_id,
                           messages.run_id, messages_fts.rank
                    FROM messages_fts JOIN messages
                    ON messages.message_id = messages_fts.message_id
                    WHERE messages_fts MATCH ? AND ({where_sql})
                    ORDER BY messages_fts.rank, messages.created_at DESC
                    LIMIT ?
                    """,
                    (match_expr, *where_values, limit),
                ).fetchall()
        except Exception:
            logger.debug("search_messages_fts failed for query %r", query, exc_info=True)
            rows = []
        if rows:
            return [
                (
                    self._message_from_row(row[:10]),
                    float(row[10]),
                    [f"message_fts_rank={float(row[10]):.4f}"],
                )
                for row in rows
            ]
        return self._search_messages_like(
            query,
            limit,
            where_sql=where_sql,
            where_values=where_values,
        )

    def _search_messages_like(
        self,
        query: str,
        limit: int,
        *,
        where_sql: str,
        where_values: list[object],
    ) -> list[tuple[StoredMessage, float, list[str]]]:
        like = f"%{query}%"
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT session_id, role, content, created_at, message_id,
                       source, peer_id, sender_id, trace_id, run_id
                FROM messages
                WHERE content LIKE ? AND ({where_sql})
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (like, *where_values, limit),
            ).fetchall()
        return [
            (
                self._message_from_row(row),
                float(index + 1),
                ["message_exact_like"],
            )
            for index, row in enumerate(rows)
        ]

    def clear_messages(self, session_id: str) -> int:
        """Delete all messages for *session_id*.

        conversation context is polluted with failing assumptions.
        context when the loop triggers ``REFLECT_AND_REPLAN`` — the next
        planner call rebuilds context from durable constraints + working
        memory snapshot only. Returns the number of deleted rows.
        """
        with connect(self.db_path) as conn:
            cur = conn.execute(
                "DELETE FROM messages WHERE session_id = ?",
                (session_id,),
            )
            return cur.rowcount

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


def _migrate_messages_table(conn) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    for column in MESSAGES_TABLE.columns:
        if column.name in existing:
            continue
        conn.execute(f"ALTER TABLE messages ADD COLUMN {_column_definition_for_alter(column)}")
    conn.execute(
        """
        UPDATE messages
        SET message_id = 'legacy:' || id
        WHERE message_id = ''
        """
    )


def _column_definition_for_alter(column: Column) -> str:
    parts = [column.name, column.sql_type]
    if not column.nullable:
        parts.append("NOT NULL")
    if column.default is not None:
        parts.append(f"DEFAULT {column.default}")
    return " ".join(parts)


def _message_identity_filter(
    *,
    session_id: str = "",
    source: str = "",
    peer_id: str = "",
    sender_id: str = "",
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    values: list[object] = []
    if session_id:
        clauses.append("messages.session_id = ?")
        values.append(session_id)
    if source and peer_id and sender_id:
        clauses.append(
            "(messages.source = ? AND messages.peer_id = ? AND messages.sender_id = ?)"
        )
        values.extend([source, peer_id, sender_id])
    return " OR ".join(clauses), values
