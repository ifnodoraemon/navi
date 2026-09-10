"""Multi-Channel Prioritized Experience Replay Buffer for Agentic Reinforcement.

Stores, prioritizes, and resamples execution traces across Weixin, Telegram,
CLI, API, and synthetic self-play environments.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import time
from typing import Any
import uuid

from .db import connect
from .dynamic_parameters import DynamicParameterRegistry
from .loop import TraceFailureDomain, TraceOutcome
from .paths import db_paths
from .schema import Column, Table, assert_schema_exact

logger = logging.getLogger(__name__)

EXPERIENCE_REPLAY_TABLE = Table(
    "experience_replay_buffer",
    [
        Column("id", "TEXT", primary_key=True),
        Column("trace_id", "TEXT", nullable=False),
        Column("channel", "TEXT", nullable=False),
        Column("prompt", "TEXT", nullable=False),
        Column("response", "TEXT", nullable=False),
        Column("reward", "REAL", nullable=False),
        Column("priority", "REAL", nullable=False),
        Column("safeguard_triggered", "INTEGER", nullable=False),
        Column("metadata_json", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
    ],
)


@dataclass(frozen=True)
class ExperienceReplayEntry:
    id: str
    trace_id: str
    channel: str
    prompt: str
    response: str
    reward: float
    priority: float
    safeguard_triggered: bool
    metadata: dict[str, Any]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "trace_id": self.trace_id,
            "channel": self.channel,
            "prompt": self.prompt,
            "response": self.response,
            "reward": self.reward,
            "priority": self.priority,
            "safeguard_triggered": self.safeguard_triggered,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


def compute_replay_priority(
    reward: float,
    *,
    alpha: float = 0.60,
    epsilon: float = 0.01,
) -> float:
    """Compute PER sampling priority: P = (|R| + epsilon)^alpha."""
    base = abs(reward) + epsilon
    return round(base ** alpha, 6)


class ExperienceReplayBuffer:
    """Prioritized Multi-Channel Experience Replay Buffer for Agentic Reinforcement."""

    def __init__(self, home: Path):
        self.home = home
        self.db_path = db_paths(home).evolution
        self.param_registry = DynamicParameterRegistry(home)
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.execute(EXPERIENCE_REPLAY_TABLE.ddl)
            assert_schema_exact(conn, EXPERIENCE_REPLAY_TABLE)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_replay_trace_id "
                "ON experience_replay_buffer(trace_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_replay_channel_priority "
                "ON experience_replay_buffer(channel, priority DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_replay_reward "
                "ON experience_replay_buffer(reward)"
            )

    def record_experience(
        self,
        *,
        trace_id: str,
        channel: str,
        prompt: str,
        response: str,
        reward: float,
        priority: float | None = None,
        safeguard_triggered: bool = False,
        metadata: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> ExperienceReplayEntry:
        current_time = time.time()
        if now is not None:
            current_time = float(now)

        alpha = self.param_registry.get("replay_priority_alpha", 0.60)
        epsilon = self.param_registry.get("replay_priority_epsilon", 0.01)

        calc_priority = compute_replay_priority(reward, alpha=alpha, epsilon=epsilon)
        if priority is not None:
            calc_priority = float(priority)

        entry_metadata: dict[str, Any] = {}
        if metadata is not None:
            entry_metadata = dict(metadata)

        entry_channel = str(channel).strip()
        if not entry_channel:
            entry_channel = "unknown"

        entry = ExperienceReplayEntry(
            id=uuid.uuid4().hex,
            trace_id=trace_id,
            channel=entry_channel,
            prompt=str(prompt),
            response=str(response),
            reward=float(reward),
            priority=float(calc_priority),
            safeguard_triggered=bool(safeguard_triggered),
            metadata=entry_metadata,
            created_at=current_time,
        )

        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO experience_replay_buffer (
                    id, trace_id, channel, prompt, response, reward,
                    priority, safeguard_triggered, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.id,
                    entry.trace_id,
                    entry.channel,
                    entry.prompt,
                    entry.response,
                    entry.reward,
                    entry.priority,
                    int(entry.safeguard_triggered),
                    json.dumps(entry.metadata, ensure_ascii=False, sort_keys=True),
                    entry.created_at,
                ),
            )

        self.prune_buffer()
        return entry

    def prune_buffer(self) -> int:
        capacity = int(self.param_registry.get("replay_buffer_capacity", 5000.0))
        total_count = self.count()
        excess = total_count - capacity
        if excess <= 0:
            return 0

        # Prune lowest priority entries, protecting golden traces (reward >= 0.8) and hard negatives (reward <= -0.8)
        deleted = 0
        with connect(self.db_path) as conn:
            cur = conn.execute(
                """
                DELETE FROM experience_replay_buffer
                WHERE id IN (
                    SELECT id FROM experience_replay_buffer
                    WHERE reward < 0.8 AND reward > -0.8
                    ORDER BY priority ASC, created_at ASC
                    LIMIT ?
                )
                """,
                (excess,),
            )
            deleted = cur.rowcount
            if deleted < excess:
                remaining_excess = excess - deleted
                cur_extra = conn.execute(
                    """
                    DELETE FROM experience_replay_buffer
                    WHERE id IN (
                        SELECT id FROM experience_replay_buffer
                        WHERE reward < 0.8
                        ORDER BY created_at ASC
                        LIMIT ?
                    )
                    """,
                    (remaining_excess,),
                )
                deleted += cur_extra.rowcount
        return deleted

    def sample_batch(
        self,
        batch_size: int = 10,
        *,
        channels: list[str] | None = None,
        min_priority: float = 0.0,
    ) -> list[ExperienceReplayEntry]:
        predicates: list[str] = ["priority >= ?"]
        params: list[Any] = [float(min_priority)]

        if channels:
            placeholders = ",".join("?" * len(channels))
            predicates.append(f"channel IN ({placeholders})")
            params.extend(channels)

        where_clause = " AND ".join(predicates)
        sql = f"""
            SELECT id, trace_id, channel, prompt, response, reward,
                   priority, safeguard_triggered, metadata_json, created_at
            FROM experience_replay_buffer
            WHERE {where_clause}
            ORDER BY priority DESC, created_at DESC
            LIMIT ?
        """
        params.append(batch_size)

        with connect(self.db_path) as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()

        return [self._row_to_entry(row) for row in rows]

    def get_golden_traces(
        self,
        limit: int = 20,
        *,
        min_reward: float = 0.8,
        channel: str | None = None,
    ) -> list[ExperienceReplayEntry]:
        predicates = ["reward >= ?"]
        params: list[Any] = [float(min_reward)]
        if channel is not None:
            predicates.append("channel = ?")
            params.append(channel)
        where_clause = " AND ".join(predicates)
        sql = f"""
            SELECT id, trace_id, channel, prompt, response, reward,
                   priority, safeguard_triggered, metadata_json, created_at
            FROM experience_replay_buffer
            WHERE {where_clause}
            ORDER BY reward DESC, priority DESC, created_at DESC
            LIMIT ?
        """
        params.append(limit)
        with connect(self.db_path) as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def get_hard_negatives(
        self,
        limit: int = 20,
        *,
        max_reward: float = -0.5,
        channel: str | None = None,
    ) -> list[ExperienceReplayEntry]:
        predicates = ["(reward <= ? OR safeguard_triggered = 1)"]
        params: list[Any] = [float(max_reward)]
        if channel is not None:
            predicates.append("channel = ?")
            params.append(channel)
        where_clause = " AND ".join(predicates)
        sql = f"""
            SELECT id, trace_id, channel, prompt, response, reward,
                   priority, safeguard_triggered, metadata_json, created_at
            FROM experience_replay_buffer
            WHERE {where_clause}
            ORDER BY priority DESC, created_at DESC
            LIMIT ?
        """
        params.append(limit)
        with connect(self.db_path) as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def count(self, channel: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM experience_replay_buffer"
        params: tuple[Any, ...] = ()
        if channel is not None:
            sql = "SELECT COUNT(*) FROM experience_replay_buffer WHERE channel = ?"
            params = (channel,)
        with connect(self.db_path) as conn:
            row = conn.execute(sql, params).fetchone()
        if row is None:
            return 0
        return int(row[0])

    def get_entry(self, entry_id: str) -> ExperienceReplayEntry | None:
        """Fetch a specific replay entry by unique ID."""
        sql = """
            SELECT id, trace_id, channel, prompt, response, reward,
                   priority, safeguard_triggered, metadata_json, created_at
            FROM experience_replay_buffer
            WHERE id = ?
        """
        with connect(self.db_path) as conn:
            row = conn.execute(sql, (entry_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_entry(row)

    def update_priority(self, entry_id: str, priority: float) -> bool:
        """Update sampling priority for a specific replay entry."""
        with connect(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE experience_replay_buffer SET priority = ? WHERE id = ?",
                (max(1e-6, float(priority)), entry_id),
            )
            return bool(cur.rowcount > 0)

    def update_priority_from_td_error(
        self,
        entry_id: str,
        td_error: float,
        *,
        alpha: float | None = None,
        epsilon: float | None = None,
    ) -> bool:
        """Update sampling priority based on new TD error: P = (|delta| + epsilon)^alpha."""
        used_alpha = self.param_registry.get("replay_priority_alpha", 0.60)
        if alpha is not None:
            used_alpha = float(alpha)
        used_epsilon = self.param_registry.get("replay_priority_epsilon", 0.01)
        if epsilon is not None:
            used_epsilon = float(epsilon)
        new_p = compute_replay_priority(td_error, alpha=used_alpha, epsilon=used_epsilon)
        return self.update_priority(entry_id, new_p)

    @staticmethod
    def _row_to_entry(row: Any) -> ExperienceReplayEntry:
        meta = {}
        try:
            meta = json.loads(row[8])
        except Exception:
            meta = {}
        return ExperienceReplayEntry(
            id=row[0],
            trace_id=row[1],
            channel=row[2],
            prompt=row[3],
            response=row[4],
            reward=float(row[5]),
            priority=float(row[6]),
            safeguard_triggered=bool(row[7]),
            metadata=meta,
            created_at=float(row[9]),
        )

    def ingest_from_trace(
        self,
        *,
        trace_id: str,
        outcome: str,
        failure_domain: str,
        events: list[Any],
        evidence: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> ExperienceReplayEntry | None:
        if not events:
            return None

        from .credit_assignment import compute_terminal_reward

        reward = compute_terminal_reward(outcome, failure_domain, param_registry=self.param_registry)

        ev: dict[str, Any] = {}
        if evidence is not None:
            ev = dict(evidence)

        # 1. Determine channel
        channel = str(ev.get("channel", "") or ev.get("source", "")).strip()
        if not channel:
            for event in events:
                src = str(getattr(event, "source", "") or "").strip()
                if src:
                    channel = src
                    break
        if not channel:
            channel = "unknown"

        # 2. Extract prompt
        prompt = str(ev.get("prompt", "") or ev.get("user_prompt", "")).strip()
        if not prompt:
            for event in events:
                phase = str(getattr(event, "phase", "") or "").lower()
                if phase in {"ingress", "user_input", "prompt", "turn.start", "planner_syscall"}:
                    msg = str(getattr(event, "message", "") or "").strip()
                    if msg:
                        prompt = msg
                        break
                    inp = str(getattr(event, "input_json", "") or "").strip()
                    if inp:
                        prompt = inp
                        break
        if not prompt:
            prompt = f"trace:{trace_id}"

        # 3. Extract response
        response = str(ev.get("response", "") or ev.get("assistant_response", "")).strip()
        if not response:
            for event in reversed(events):
                phase = str(getattr(event, "phase", "") or "").lower()
                if phase in {"egress", "assistant", "planner.decision", "loop.decision", "capability_result"}:
                    msg = str(getattr(event, "message", "") or "").strip()
                    if msg:
                        response = msg
                        break
                    out = str(getattr(event, "output_json", "") or "").strip()
                    if out:
                        response = out
                        break
        if not response:
            response = f"outcome:{outcome}:{failure_domain}"

        # 4. Check safeguard trigger
        safeguard_triggered = (failure_domain == str(TraceFailureDomain.SAFEGUARD_POLICY))
        if not safeguard_triggered:
            for event in events:
                tool_name = str(getattr(event, "tool", "") or "").lower()
                phase_name = str(getattr(event, "phase", "") or "").lower()
                if "safeguard" in tool_name or "safeguard" in phase_name:
                    if not bool(getattr(event, "ok", True)):
                        safeguard_triggered = True
                        break

        meta: dict[str, Any] = {
            "outcome": outcome,
            "failure_domain": failure_domain,
            "event_count": len(events),
            "evidence": ev,
        }

        return self.record_experience(
            trace_id=trace_id,
            channel=channel,
            prompt=prompt,
            response=response,
            reward=reward,
            safeguard_triggered=safeguard_triggered,
            metadata=meta,
            now=now,
        )
