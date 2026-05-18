from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoredMessage:
    session_id: str
    role: str
    content: str
    created_at: float


@dataclass(frozen=True)
class MemoryItem:
    id: str
    type: str
    status: str
    scope: str
    content: str
    source: str
    confidence: float
    created_at: float
    updated_at: float
    last_verified_at: float
    expires_at: float
    metadata: dict


MEMORY_TYPES = {
    "working",
    "constraint",
    "episode",
    "semantic",
    "fact",
    "procedural",
    "preference",
    "negative",
    "skill",
    "hypothesis",
}
MEMORY_STATUSES = {"proposed", "accepted", "active", "contradicted", "stale", "archived", "revoked"}
ACTIVE_STATUSES = {"accepted", "active"}
TYPE_PRIORITY = {
    "constraint": 100,
    "negative": 90,
    "working": 85,
    "preference": 70,
    "procedural": 65,
    "skill": 60,
    "semantic": 55,
    "fact": 55,
    "hypothesis": 25,
    "episode": 15,
}


class MemoryStore:
    def __init__(self, home: Path):
        self.home = home
        self.memory_dir = home / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = home / "sessions.db"
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)")
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_status ON memory_items(status, type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_items(scope)")

    def read_memory(self) -> str:
        return self.render_context(query="")

    def append_memory(self, text: str) -> None:
        self.add_item(
            "fact",
            text,
            source="manual",
            scope="global",
            status="active",
            confidence=0.7,
        )

    def add_item(
        self,
        memory_type: str,
        content: str,
        *,
        source: str,
        scope: str = "global",
        status: str = "proposed",
        confidence: float = 0.5,
        last_verified_at: float | None = None,
        expires_at: float = 0.0,
        metadata: dict | None = None,
    ) -> MemoryItem:
        memory_type = memory_type.strip().lower()
        status = status.strip().lower()
        if memory_type not in MEMORY_TYPES:
            raise ValueError(f"Unsupported memory type: {memory_type}")
        if status not in MEMORY_STATUSES:
            raise ValueError(f"Unsupported memory status: {status}")
        now = time.time()
        item = MemoryItem(
            id=uuid.uuid4().hex,
            type=memory_type,
            status=status,
            scope=scope.strip() or "global",
            content=content.strip(),
            source=source.strip() or "unknown",
            confidence=max(0.0, min(1.0, confidence)),
            created_at=now,
            updated_at=now,
            last_verified_at=last_verified_at or 0.0,
            expires_at=expires_at,
            metadata=metadata or {},
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO memory_items(
                    id, type, status, scope, content, source, confidence,
                    created_at, updated_at, last_verified_at, expires_at, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
        return item

    def list_items(
        self,
        *,
        memory_type: str | None = None,
        status: str | None = None,
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
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT id, type, status, scope, content, source, confidence,
                       created_at, updated_at, last_verified_at, expires_at, metadata
                FROM memory_items
                {where}
                ORDER BY updated_at DESC LIMIT ?
                """,
                values,
            ).fetchall()
        return [self._item_from_row(row) for row in rows]

    def get_item(self, item_id: str) -> MemoryItem | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, type, status, scope, content, source, confidence,
                       created_at, updated_at, last_verified_at, expires_at, metadata
                FROM memory_items WHERE id = ?
                """,
                (item_id,),
            ).fetchone()
        return self._item_from_row(row) if row else None

    def set_status(self, item_id: str, status: str) -> MemoryItem | None:
        status = status.strip().lower()
        if status not in MEMORY_STATUSES:
            raise ValueError(f"Unsupported memory status: {status}")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE memory_items SET status = ?, updated_at = ? WHERE id = ?",
                (status, time.time(), item_id),
            )
        return self.get_item(item_id)

    def recall(self, query: str, *, limit: int = 8) -> list[MemoryItem]:
        now = time.time()
        candidates = [
            item
            for item in self.list_items(limit=500)
            if item.status in ACTIVE_STATUSES and (not item.expires_at or item.expires_at > now)
        ]
        scored = [(self._score(item, query), item) for item in candidates]
        scored = [(score, item) for score, item in scored if score > 0]
        scored.sort(key=lambda pair: (pair[0], pair[1].updated_at), reverse=True)
        return [item for _, item in scored[:limit]]

    def render_context(self, query: str, *, limit: int = 8) -> str:
        items = self.recall(query, limit=limit)
        if not items:
            return ""
        lines = ["Memory recall:"]
        for item in items:
            verified = time.strftime("%Y-%m-%d", time.localtime(item.last_verified_at)) if item.last_verified_at else "unverified"
            lines.append(
                f"- [{item.type} status={item.status} scope={item.scope} confidence={item.confidence:.2f} "
                f"source={item.source} verified={verified} id={item.id}] {item.content}"
            )
        return "\n".join(lines)

    def new_session_id(self) -> str:
        return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]

    def add_message(self, session_id: str, role: str, content: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO messages(session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_id, role, content, time.time()),
            )

    def list_sessions(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT session_id FROM messages GROUP BY session_id ORDER BY MAX(created_at) DESC"
            ).fetchall()
        return [row[0] for row in rows]

    def get_messages(self, session_id: str, limit: int = 50) -> list[StoredMessage]:
        with sqlite3.connect(self.db_path) as conn:
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

    @staticmethod
    def _item_from_row(row: tuple) -> MemoryItem:
        values = list(row)
        values[11] = json.loads(values[11] or "{}")
        return MemoryItem(*values)

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {part.lower() for part in text.replace("/", " ").replace("_", " ").split() if len(part) >= 2}

    @classmethod
    def _score(cls, item: MemoryItem, query: str) -> float:
        priority = TYPE_PRIORITY.get(item.type, 10)
        if item.type == "constraint":
            priority += 50
        query_tokens = cls._tokens(query)
        content_tokens = cls._tokens(f"{item.scope} {item.content}")
        overlap = len(query_tokens & content_tokens)
        if query_tokens and not overlap and item.type not in {"constraint", "working"}:
            return 0
        freshness = min(10.0, max(0.0, (item.updated_at - 1_700_000_000) / 10_000_000))
        return priority + (overlap * 12) + (item.confidence * 10) + freshness
