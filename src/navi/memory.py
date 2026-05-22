from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .db import connect
from .json_utils import parse_first_json_object
from .text_utils import truncate_middle

if TYPE_CHECKING:
    from .provider import ModelPool
    from .tasks import Task, ExecutionLog


@dataclass(frozen=True)
class StoredMessage:
    session_id: str
    role: str
    content: str
    created_at: float


@dataclass(frozen=True)
class SessionAlias:
    alias: str
    session_id: str
    created_at: float
    updated_at: float


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
LEARNABLE_MEMORY_TYPES = ("preference", "constraint", "negative", "fact", "semantic")
MEMORY_STATUSES = {"proposed", "accepted", "active", "contradicted", "stale", "archived", "revoked"}
ACTIVE_STATUSES = {"accepted", "active"}
TASK_LEARNING_LOG_LIMIT = 3000
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
        legacy_db_path = home / "sessions.db"
        self.db_path = home / "memory.db"
        if legacy_db_path.exists() and not self.db_path.exists():
            legacy_db_path.replace(self.db_path)
        self._init_db()
        self._session_locks = {}

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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)")
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
        with connect(self.db_path) as conn:
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
        with connect(self.db_path) as conn:
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
        with connect(self.db_path) as conn:
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
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE memory_items SET status = ?, updated_at = ? WHERE id = ?",
                (status, time.time(), item_id),
            )
        return self.get_item(item_id)

    def delete_item(self, item_id: str) -> None:
        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM memory_items WHERE id = ?", (item_id,))

    def restore_item(self, item_dict: dict) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO memory_items(
                    id, type, status, scope, content, source, confidence,
                    created_at, updated_at, last_verified_at, expires_at, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_dict["id"],
                    item_dict["type"],
                    item_dict["status"],
                    item_dict["scope"],
                    item_dict["content"],
                    item_dict["source"],
                    item_dict["confidence"],
                    item_dict["created_at"],
                    item_dict["updated_at"],
                    item_dict["last_verified_at"],
                    item_dict["expires_at"],
                    json.dumps(item_dict["metadata"], sort_keys=True) if isinstance(item_dict["metadata"], dict) else item_dict["metadata"],
                ),
            )

    def _parse_json_learnings(self, response_raw: str) -> list[dict]:
        data = parse_first_json_object(response_raw)
        if not isinstance(data, dict):
            return []

        learnings = data.get("learnings")
        if not isinstance(learnings, list):
            return []
        return learnings


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

    def create_session(self, *, alias: str | None = None) -> str:
        session_id = self.new_session_id()
        if alias:
            self.set_session_alias(alias, session_id)
        return session_id

    def set_session_alias(self, alias: str, session_id: str) -> SessionAlias:
        now = time.time()
        existing = self.get_session_alias(alias)
        created_at = existing.created_at if existing else now
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO session_aliases(alias, session_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(alias) DO UPDATE SET session_id = excluded.session_id, updated_at = excluded.updated_at
                """,
                (alias, session_id, created_at, now),
            )
        return self.get_session_alias(alias) or SessionAlias(alias, session_id, created_at, now)

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

    def current_session_id(self, alias: str) -> str:
        current = self.get_session_alias(alias)
        if current:
            return current.session_id
        return self.create_session(alias=alias)

    def rotate_session(self, alias: str) -> SessionAlias:
        return self.set_session_alias(alias, self.new_session_id())

    def list_session_aliases(self, *, limit: int = 50) -> list[SessionAlias]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT alias, session_id, created_at, updated_at
                FROM session_aliases ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [SessionAlias(*row) for row in rows]

    def add_message(self, session_id: str, role: str, content: str) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO messages(session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (session_id, role, content, time.time()),
            )

    def list_sessions(self) -> list[str]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT session_id FROM messages GROUP BY session_id ORDER BY MAX(created_at) DESC"
            ).fetchall()
        return [row[0] for row in rows]

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

    async def extract_and_consolidate_memories(
        self,
        session_id: str,
        provider: ModelPool,
        task_id: str = "",
    ) -> list[MemoryItem]:
        from .provider import ChatMessage
        from .evolution import EvolutionLedger

        if session_id not in self._session_locks:
            self._session_locks[session_id] = [asyncio.Lock(), 0]
        
        lock_info = self._session_locks[session_id]
        lock_info[1] += 1
        lock = lock_info[0]

        try:
            async with lock:
                # 1. Fetch recent messages in this session
                messages = self.get_messages(session_id, limit=6)
                if not messages:
                    return []

                # 2. Fetch existing active memory items
                active_items = []
                for item_type in LEARNABLE_MEMORY_TYPES:
                    active_items.extend(self.list_items(memory_type=item_type, status="active", limit=100))

                # 3. Format context
                conversation_text = "\n".join(f"{msg.role}: {msg.content}" for msg in messages)
                memories_data = [
                    {
                        "id": item.id,
                        "type": item.type,
                        "content": item.content,
                        "confidence": item.confidence,
                        "source": item.source,
                    }
                    for item in active_items
                ]
                memories_text = json.dumps(memories_data, ensure_ascii=False, indent=2)

                # 4. Prompts
                system_prompt = (
                    "You are Navi's memory consolidator and learning agent.\n"
                    "Your job is to analyze the recent conversation turn and existing active memories, and decide:\n"
                    "1. If any new durable facts, user preferences, negative lessons (avoiding repetitive failures), or constraints should be learned.\n"
                    "2. If any existing active memories are now updated, contradicted, or should be revoked.\n\n"
                    "Rules:\n"
                    "- Only extract genuinely durable, useful information. Do NOT extract standard conversational greetings, temporary commands, or trivial details.\n"
                    "- Avoid adding duplicate memories that already exist in the list.\n"
                    "- If a new preference or fact contradicts an existing active memory, revoke the old one and add the new one.\n"
                    "- Output ONLY a valid JSON object matching the schema below. No prose, no markdown fences.\n\n"
                    "JSON Schema:\n"
                    "{\n"
                    "  \"learnings\": [\n"
                    "    {\n"
                    "      \"action\": \"add\",\n"
                    f"      \"type\": \"{'|'.join(LEARNABLE_MEMORY_TYPES)}\",\n"
                    "      \"content\": \"durable fact, preference, negative lesson, or constraint (in the user's language)\",\n"
                    "      \"confidence\": 0.8\n"
                    "    },\n"
                    "    {\n"
                    "      \"action\": \"revoke\",\n"
                    "      \"id\": \"existing_memory_id\",\n"
                    "      \"reason\": \"explanation of why it is revoked/contradicted\"\n"
                    "    }\n"
                    "  ]\n"
                    "}"
                )
                user_prompt = (
                    f"Existing Active Memories:\n{memories_text}\n\n"
                    f"Recent Conversation Turn:\n{conversation_text}\n\n"
                    "Analyze and output the JSON learnings:"
                )

                chat_messages = [
                    ChatMessage("system", system_prompt),
                    ChatMessage("user", user_prompt),
                ]

                try:
                    response_raw = await provider.complete_for("planner", chat_messages)
                except Exception:
                    return []

                # 5. Extract JSON object
                learnings = self._parse_json_learnings(response_raw)

                ledger = EvolutionLedger(self.home)
                affected_items = []
                seen_memory_keys = {
                    (item.type, item.content.strip().lower())
                    for item in active_items
                }
                for learning in learnings:
                    if not isinstance(learning, dict):
                        continue
                    action = str(learning.get("action", "")).strip().lower()
                    if action == "add":
                        m_type = str(learning.get("type", "")).strip().lower()
                        content = str(learning.get("content", "")).strip()
                        if not content or m_type not in LEARNABLE_MEMORY_TYPES:
                            continue
                        memory_key = (m_type, content.lower())
                        if memory_key in seen_memory_keys:
                            continue

                        try:
                            conf_val = float(learning.get("confidence", 0.7))
                        except (ValueError, TypeError):
                            conf_val = 0.7

                        new_item = self.add_item(
                            memory_type=m_type,
                            content=content,
                            source="evolution",
                            status="active",
                            confidence=conf_val,
                        )
                        seen_memory_keys.add(memory_key)
                        affected_items.append(new_item)
                        ledger.record(
                            task_id=task_id or f"session:{session_id}",
                            target_type="memory_item",
                            target_id=new_item.id,
                            reason=f"Extracted learning: {new_item.content[:60]}",
                            before="",
                            after=json.dumps(new_item.__dict__, default=str),
                        )
                    elif action == "revoke":
                        item_id = str(learning.get("id", "")).strip()
                        if not item_id:
                            continue
                        old_item = self.get_item(item_id)
                        if old_item and old_item.status in ["active", "accepted"]:
                            updated_item = self.set_status(item_id, "revoked")
                            if updated_item:
                                affected_items.append(updated_item)
                            ledger.record(
                                task_id=task_id or f"session:{session_id}",
                                target_type="memory_item",
                                target_id=item_id,
                                reason=str(learning.get("reason", "Revoked by consolidation")),
                                before=json.dumps(old_item.__dict__, default=str),
                                after="revoked",
                            )
                return affected_items
        finally:
            if session_id in self._session_locks:
                self._session_locks[session_id][1] -= 1
                if self._session_locks[session_id][1] <= 0:
                    self._session_locks.pop(session_id, None)

    async def extract_memories_from_task(
        self,
        task: Task,
        logs: list[ExecutionLog],
        provider: ModelPool,
    ) -> list[MemoryItem]:
        from .provider import ChatMessage
        from .evolution import EvolutionLedger

        # 1. Fetch existing active memory items
        active_items = []
        for item_type in LEARNABLE_MEMORY_TYPES:
            active_items.extend(self.list_items(memory_type=item_type, status="active", limit=100))

        # 2. Format execution logs
        logs_text_parts = []
        for log in logs[-10:]:
            logs_text_parts.append(
                f"Phase: {log.phase}\n"
                f"Command: {log.command}\n"
                f"Exit Code: {log.exit_code}\n"
                f"Stdout: {truncate_middle(log.stdout, TASK_LEARNING_LOG_LIMIT)}\n"
                f"Stderr: {truncate_middle(log.stderr, TASK_LEARNING_LOG_LIMIT)}"
            )
        logs_text = "\n---\n".join(logs_text_parts)

        # 3. Format context
        task_context = (
            f"Task Prompt: {task.prompt}\n"
            f"Status: {task.status}\n"
            f"Plan Summary: {task.plan_summary}\n"
            f"Result Summary: {task.result_summary}\n"
            f"Error: {task.error}\n"
            f"Execution Logs:\n{logs_text}"
        )
        memories_data = [
            {
                "id": item.id,
                "type": item.type,
                "content": item.content,
                "confidence": item.confidence,
                "source": item.source,
            }
            for item in active_items
        ]
        memories_text = json.dumps(memories_data, ensure_ascii=False, indent=2)

        # 4. Prompts
        system_prompt = (
            "You are Navi's memory consolidator and task learning agent.\n"
            "Your job is to analyze a completed local execution task and its logs, alongside existing active memories, and decide:\n"
            "1. If any new durable facts, user preferences, negative lessons (e.g. command syntax that failed, directory paths that were missing), or constraints should be learned.\n"
            "2. If any existing active memories are now updated, contradicted, or should be revoked.\n\n"
            "Rules:\n"
            "- Focus heavily on 'negative' memory for failed steps, to prevent future execution tools from repeating the same mistake.\n"
            "- Only extract genuinely durable, useful technical or user preference facts. Do NOT extract standard task markers or temporary files.\n"
            "- Avoid adding duplicate memories that already exist in the list.\n"
            "- If a new learning contradicts an existing active memory, revoke the old one.\n"
            "- Output ONLY a valid JSON object matching the schema below. No prose, no markdown fences.\n\n"
            "JSON Schema:\n"
            "{\n"
            "  \"learnings\": [\n"
            "    {\n"
            "      \"action\": \"add\",\n"
            f"      \"type\": \"{'|'.join(LEARNABLE_MEMORY_TYPES)}\",\n"
            "      \"content\": \"durable fact, preference, negative lesson, or constraint (in the user's language)\",\n"
            "      \"confidence\": 0.8\n"
            "    },\n"
            "    {\n"
            "      \"action\": \"revoke\",\n"
            "      \"id\": \"existing_memory_id\",\n"
            "      \"reason\": \"explanation of why it is revoked/contradicted\"\n"
            "    }\n"
            "  ]\n"
            "}"
        )
        user_prompt = (
            f"Existing Active Memories:\n{memories_text}\n\n"
            f"Task Execution Outcome:\n{task_context}\n\n"
            "Analyze and output the JSON learnings:"
        )

        chat_messages = [
            ChatMessage("system", system_prompt),
            ChatMessage("user", user_prompt),
        ]

        try:
            response_raw = await provider.complete_for("planner", chat_messages)
        except Exception:
            return []

        # 5. Extract JSON object
        learnings = self._parse_json_learnings(response_raw)

        ledger = EvolutionLedger(self.home)
        affected_items = []
        seen_memory_keys = {
            (item.type, item.content.strip().lower())
            for item in active_items
        }
        for learning in learnings:
            if not isinstance(learning, dict):
                continue
            action = str(learning.get("action", "")).strip().lower()
            if action == "add":
                m_type = str(learning.get("type", "")).strip().lower()
                content = str(learning.get("content", "")).strip()
                if not content or m_type not in LEARNABLE_MEMORY_TYPES:
                    continue
                memory_key = (m_type, content.lower())
                if memory_key in seen_memory_keys:
                    continue

                try:
                    conf_val = float(learning.get("confidence", 0.7))
                except (ValueError, TypeError):
                    conf_val = 0.7

                new_item = self.add_item(
                    memory_type=m_type,
                    content=content,
                    source="evolution",
                    status="active",
                    confidence=conf_val,
                )
                seen_memory_keys.add(memory_key)
                affected_items.append(new_item)
                ledger.record(
                    task_id=task.id,
                    target_type="memory_item",
                    target_id=new_item.id,
                    reason=f"Extracted learning: {new_item.content[:60]}",
                    before="",
                    after=json.dumps(new_item.__dict__, default=str),
                )
            elif action == "revoke":
                item_id = str(learning.get("id", "")).strip()
                if not item_id:
                    continue
                old_item = self.get_item(item_id)
                if old_item and old_item.status in ["active", "accepted"]:
                    updated_item = self.set_status(item_id, "revoked")
                    if updated_item:
                        affected_items.append(updated_item)
                    ledger.record(
                        task_id=task.id,
                        target_type="memory_item",
                        target_id=item_id,
                        reason=str(learning.get("reason", "Revoked by consolidation")),
                        before=json.dumps(old_item.__dict__, default=str),
                        after="revoked",
                    )
        return affected_items

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
