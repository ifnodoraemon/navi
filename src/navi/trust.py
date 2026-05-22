from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .db import connect
from .json_utils import parse_first_json_object
from typing import Any, TYPE_CHECKING

from .tasks import Task

if TYPE_CHECKING:
    from .provider import ModelPool

logger = logging.getLogger("navi.trust")


LEVELS = ["L0", "L1", "L2", "L3", "L4"]
SEMANTIC_RULE_BATCH_SIZE = 5
LEVEL_LABELS = {
    "L0": "observe",
    "L1": "suggest",
    "L2": "approve_execute",
    "L3": "trusted_auto",
    "L4": "broad_delegate",
}


@dataclass(frozen=True)
class TrustRule:
    id: str
    name: str
    pattern: str
    project_path: str
    sender_id: str
    autonomy_level: str
    success_count: int
    failure_count: int
    data: dict[str, Any]
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class TrustDecision:
    level: str
    action: str
    rule_id: str
    why: str
    trusted_project: bool


class TrustStore:
    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = home / "trust.db"
        self._semantic_sem: asyncio.Semaphore | None = None
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trust_rules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    pattern TEXT NOT NULL,
                    project_path TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    autonomy_level TEXT NOT NULL,
                    success_count INTEGER NOT NULL,
                    failure_count INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(pattern, project_path, sender_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trust_sender ON trust_rules(sender_id)")

    async def decide(self, *, prompt: str, sender_id: str, workspace: str, provider: ModelPool | None = None) -> TrustDecision:
        rule = await self.match(prompt=prompt, sender_id=sender_id, workspace=workspace, provider=provider)
        if rule is None:
            return TrustDecision(
                level="L2",
                action="approval",
                rule_id="",
                why="No matching trust rule yet; Navi will plan first and ask for approval.",
                trusted_project=False,
            )
        action = "auto_execute" if rule.autonomy_level == "L3" and rule.project_path else "approval"
        if rule.autonomy_level in {"L0", "L1"}:
            action = "suggest"
        if rule.autonomy_level == "L4":
            action = "approval"
        return TrustDecision(
            level=rule.autonomy_level,
            action=action,
            rule_id=rule.id,
            why=f"Matched trust rule {rule.name} at {rule.autonomy_level}.",
            trusted_project=bool(rule.project_path),
        )

    async def match(self, *, prompt: str, sender_id: str, workspace: str, provider: ModelPool | None = None) -> TrustRule | None:
        workspace = self._normalize_project_path(workspace)
        rules = await asyncio.to_thread(self.list, sender_id=sender_id)
        candidates = [
            rule
            for rule in rules
            if not rule.project_path or self._normalize_project_path(rule.project_path) == workspace
        ]
        if not candidates:
            return None
        
        # 1. First check candidates synchronously using token-based matching to avoid LLM calls
        pattern_matches = [
            rule for rule in candidates if self._pattern_matches(rule.pattern, prompt)
        ]
        if pattern_matches:
            pattern_matches.sort(key=lambda rule: (LEVELS.index(rule.autonomy_level), rule.updated_at), reverse=True)
            return pattern_matches[0]
            
        # 2. Fall back to semantic matching with Semaphore concurrency limits
        if not provider:
            return None
            
        async def sem_semantic_match(rule: TrustRule) -> tuple[TrustRule, bool]:
            async with self._semantic_semaphore():
                res = await self._semantic_match(rule.pattern, prompt, provider)
                return rule, res
                
        semantic_candidates = sorted(
            candidates,
            key=lambda rule: (rule.success_count, rule.updated_at),
            reverse=True,
        )
        matching_rules: list[TrustRule] = []
        for start in range(0, len(semantic_candidates), SEMANTIC_RULE_BATCH_SIZE):
            batch = semantic_candidates[start:start + SEMANTIC_RULE_BATCH_SIZE]
            tasks = [sem_semantic_match(rule) for rule in batch]
            results = await asyncio.gather(*tasks)
            matching_rules = [rule for rule, m in results if m]
            if matching_rules:
                break
                
        if not matching_rules:
            return None
            
        matching_rules.sort(key=lambda rule: (LEVELS.index(rule.autonomy_level), rule.updated_at), reverse=True)
        return matching_rules[0]

    def _semantic_semaphore(self) -> asyncio.Semaphore:
        if self._semantic_sem is None:
            self._semantic_sem = asyncio.Semaphore(2)
        return self._semantic_sem

    async def _semantic_match(self, pattern: str, prompt: str, provider: ModelPool | None = None) -> bool:
        if self._pattern_matches(pattern, prompt):
            return True
        if not provider:
            return False
        from .provider import ChatMessage
        messages = [
            ChatMessage(
                role="system",
                content=(
                    "You are Navi's Trust Engine classifier.\n"
                    "Your task is to determine whether a given user task prompt semantically matches a specific trust rule pattern.\n\n"
                    "Evaluate if the user's intent is conceptually/semantically covered by the trust rule pattern.\n"
                    "Respond ONLY with a JSON object:\n"
                    "{\n"
                    '  "matches": true or false,\n'
                    '  "reason": "a brief explanation"\n'
                    "}"
                )
            ),
            ChatMessage(
                role="user",
                content=f"Trust Rule Pattern: {pattern}\nUser Task Prompt: {prompt}"
            )
        ]
        try:
            response_text = await provider.complete_for(role="planner", messages=messages)
            data = parse_first_json_object(response_text)
            if data:
                return bool(data.get("matches", False))
        except Exception as e:
            logger.debug("Semantic trust match failed: %s", e, exc_info=True)
        return False

    def record_success(self, task: Task) -> TrustRule:
        rule = self.get(task.trust_rule_id) if task.trust_rule_id else None
        if rule is None:
            rule = self.upsert(
                name=self._rule_name(task),
                pattern=self._pattern(task.prompt),
                project_path=task.workspace if task.autonomy_level == "L3" else "",
                sender_id=task.sender_id,
                autonomy_level=task.autonomy_level or "L2",
                data={"last_task_id": task.id, "auto_created": True},
            )
        new_success = rule.success_count + 1
        consecutive_successes = rule.data.get("consecutive_successes", 0) + 1
        new_level = rule.autonomy_level
        project_path = rule.project_path
        if consecutive_successes >= 3:
            current_index = LEVELS.index(rule.autonomy_level)
            if current_index < LEVELS.index("L3"):
                if (current_index + 1) < LEVELS.index("L3") or (task.workspace or project_path):
                    new_level = LEVELS[current_index + 1]
                    consecutive_successes = 0
                    if new_level == "L3" and not project_path:
                        project_path = task.workspace
        updated_data = {"consecutive_successes": consecutive_successes}
        return self._update_counts(
            rule.id,
            success_count=new_success,
            failure_count=rule.failure_count,
            autonomy_level=new_level,
            project_path=project_path,
            reason=f"Successful task {task.id}",
            data=updated_data,
        )

    async def record_failure(self, task: Task) -> TrustRule | None:
        rule = self.get(task.trust_rule_id) if task.trust_rule_id else await self.match(
            prompt=task.prompt,
            sender_id=task.sender_id,
            workspace=task.workspace,
        )
        if rule is None:
            return None
        current_index = LEVELS.index(rule.autonomy_level)
        new_level = LEVELS[max(0, current_index - 1)]
        return self._update_counts(
            rule.id,
            success_count=rule.success_count,
            failure_count=rule.failure_count + 1,
            autonomy_level=new_level,
            project_path=rule.project_path,
            reason=f"Failed task {task.id}",
            data={"consecutive_successes": 0},
        )

    def upsert(
        self,
        *,
        name: str,
        pattern: str,
        project_path: str,
        sender_id: str,
        autonomy_level: str,
        data: dict[str, Any] | None = None,
    ) -> TrustRule:
        now = time.time()
        project_path = self._normalize_project_path(project_path) if project_path else ""
        existing = self._get_unique(pattern, project_path, sender_id)
        payload = data or {}
        if existing:
            merged = {**existing.data, **payload}
            with connect(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE trust_rules
                    SET name = ?, autonomy_level = ?, data = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (name, autonomy_level, json.dumps(merged, sort_keys=True), now, existing.id),
                )
            return self.get(existing.id) or existing
        rule = TrustRule(
            id=uuid.uuid4().hex,
            name=name,
            pattern=pattern,
            project_path=project_path,
            sender_id=sender_id,
            autonomy_level=autonomy_level,
            success_count=0,
            failure_count=0,
            data=payload,
            created_at=now,
            updated_at=now,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO trust_rules(
                    id, name, pattern, project_path, sender_id, autonomy_level,
                    success_count, failure_count, data, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule.id,
                    rule.name,
                    rule.pattern,
                    rule.project_path,
                    rule.sender_id,
                    rule.autonomy_level,
                    rule.success_count,
                    rule.failure_count,
                    json.dumps(rule.data, sort_keys=True),
                    rule.created_at,
                    rule.updated_at,
                ),
            )
        return rule

    def get(self, rule_id: str) -> TrustRule | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, name, pattern, project_path, sender_id, autonomy_level,
                       success_count, failure_count, data, created_at, updated_at
                FROM trust_rules WHERE id = ?
                """,
                (rule_id,),
            ).fetchone()
        return self._rule_from_row(row) if row else None

    def list(self, *, sender_id: str | None = None, limit: int = 100) -> list[TrustRule]:
        with connect(self.db_path) as conn:
            if sender_id:
                rows = conn.execute(
                    """
                    SELECT id, name, pattern, project_path, sender_id, autonomy_level,
                           success_count, failure_count, data, created_at, updated_at
                    FROM trust_rules WHERE sender_id = ? ORDER BY updated_at DESC LIMIT ?
                    """,
                    (sender_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, name, pattern, project_path, sender_id, autonomy_level,
                           success_count, failure_count, data, created_at, updated_at
                    FROM trust_rules ORDER BY updated_at DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [self._rule_from_row(row) for row in rows]

    def set_level(self, rule_id: str, level: str) -> TrustRule | None:
        if level not in LEVELS:
            raise ValueError(f"Unsupported autonomy level: {level}")
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE trust_rules SET autonomy_level = ?, updated_at = ? WHERE id = ?",
                (level, time.time(), rule_id),
            )
        return self.get(rule_id)

    def restore(self, payload: dict[str, Any]) -> TrustRule:
        rule_id = str(payload["id"])
        now = time.time()
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        existing = self.get(rule_id)
        project_path = self._normalize_project_path(str(payload.get("project_path", ""))) if payload.get("project_path") else ""
        with connect(self.db_path) as conn:
            if existing:
                conn.execute(
                    """
                    UPDATE trust_rules
                    SET name = ?, pattern = ?, project_path = ?, sender_id = ?,
                        autonomy_level = ?, success_count = ?, failure_count = ?,
                        data = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        payload.get("name", ""),
                        payload.get("pattern", ""),
                        project_path,
                        payload.get("sender_id", ""),
                        payload.get("autonomy_level", "L2"),
                        int(payload.get("success_count", 0)),
                        int(payload.get("failure_count", 0)),
                        json.dumps(data, sort_keys=True),
                        now,
                        rule_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO trust_rules(
                        id, name, pattern, project_path, sender_id, autonomy_level,
                        success_count, failure_count, data, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rule_id,
                        payload.get("name", ""),
                        payload.get("pattern", ""),
                        project_path,
                        payload.get("sender_id", ""),
                        payload.get("autonomy_level", "L2"),
                        int(payload.get("success_count", 0)),
                        int(payload.get("failure_count", 0)),
                        json.dumps(data, sort_keys=True),
                        now,
                        now,
                    ),
                )
        restored = self.get(rule_id)
        if restored is None:
            raise RuntimeError(f"Failed to restore trust rule: {rule_id}")
        return restored

    def delete(self, rule_id: str) -> None:
        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM trust_rules WHERE id = ?", (rule_id,))

    def _get_unique(self, pattern: str, project_path: str, sender_id: str) -> TrustRule | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, name, pattern, project_path, sender_id, autonomy_level,
                       success_count, failure_count, data, created_at, updated_at
                FROM trust_rules
                WHERE pattern = ? AND project_path = ? AND sender_id = ?
                """,
                (pattern, project_path, sender_id),
            ).fetchone()
        return self._rule_from_row(row) if row else None

    def _update_counts(
        self,
        rule_id: str,
        *,
        success_count: int,
        failure_count: int,
        autonomy_level: str,
        project_path: str,
        reason: str,
        data: dict[str, Any] | None = None,
    ) -> TrustRule:
        rule = self.get(rule_id)
        if rule is None:
            raise ValueError(f"Unknown trust rule: {rule_id}")
        base_data = rule.data
        if data:
            base_data = {**base_data, **data}
        final_data = {**base_data, "last_reason": reason}
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE trust_rules
                SET success_count = ?, failure_count = ?, autonomy_level = ?,
                    project_path = ?, data = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    success_count,
                    failure_count,
                    autonomy_level,
                    project_path,
                    json.dumps(final_data, sort_keys=True),
                    time.time(),
                    rule_id,
                ),
            )
        return self.get(rule_id) or rule

    @staticmethod
    def _rule_from_row(row: tuple) -> TrustRule:
        return TrustRule(
            id=row[0],
            name=row[1],
            pattern=row[2],
            project_path=row[3],
            sender_id=row[4],
            autonomy_level=row[5],
            success_count=row[6],
            failure_count=row[7],
            data=json.loads(row[8] or "{}"),
            created_at=row[9],
            updated_at=row[10],
        )

    @staticmethod
    def _pattern(prompt: str) -> str:
        words = [word.strip(".,:;!?").lower() for word in prompt.split() if len(word.strip(".,:;!?")) > 3]
        return " ".join(words[:3]) or prompt[:24].lower() or "task"

    @staticmethod
    def _rule_name(task: Task) -> str:
        return f"{task.sender_id or 'local'}:{TrustStore._pattern(task.prompt)}"

    @staticmethod
    def _pattern_matches(pattern: str, prompt: str) -> bool:
        pattern_tokens = TrustStore._tokens(pattern)
        if not pattern_tokens:
            return False
        prompt_tokens = TrustStore._tokens(prompt)
        return pattern_tokens <= prompt_tokens

    @staticmethod
    def _tokens(text: str) -> set[str]:
        separators = "/_-.,:;!?\n\t"
        normalized = text.lower()
        for separator in separators:
            normalized = normalized.replace(separator, " ")
        return {part for part in normalized.split() if len(part) > 2}

    @staticmethod
    def _normalize_project_path(path: str) -> str:
        if not path:
            return ""
        return str(Path(path).expanduser().resolve())
