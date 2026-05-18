from __future__ import annotations

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

    def read_memory(self) -> str:
        parts = []
        for name in ("USER.md", "MEMORY.md"):
            path = self.memory_dir / name
            if path.exists():
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    parts.append(f"# {name}\n{content}")
        return "\n\n".join(parts)

    def append_memory(self, text: str) -> None:
        path = self.memory_dir / "MEMORY.md"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        path.write_text(f"{existing.rstrip()}\n\n- {timestamp}: {text.strip()}\n".lstrip(), encoding="utf-8")

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
