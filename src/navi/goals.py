from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect, ensure_schema_version
from .lifecycle import (
    RUN_STATUS_PENDING,
    RUN_STATUS_FAILED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_FAILED,
)
from .paths import db_paths
from .runs import Run
from .schema import Column, Table, assert_schema_exact

GOAL_STORE_SCHEMA_VERSION = 1

GOAL_STATUS_ACTIVE = "active"
GOAL_STATUS_AWAITING_APPROVAL = "awaiting_approval"
GOAL_STATUS_VERIFIED_COMPLETE = "verified_complete"
GOAL_STATUS_BLOCKED = "blocked"
GOAL_STATUS_REJECTED = "rejected"


def _require_workspace(workspace: str) -> str:
    value = workspace.strip()
    if not value:
        raise ValueError("workspace is required")
    return value


@dataclass(frozen=True)
class Goal:
    id: str
    objective: str
    status: str
    source: str
    peer_id: str
    sender_id: str
    session_id: str
    workspace: str
    run_id: str
    trace_id: str
    evidence_json: str
    blocked_reason: str
    stop_condition: str
    timeout: float
    max_retries: int
    created_at: float
    updated_at: float
    completed_at: float


GOAL_STATUSES = {
    "active",
    "completed",
    "failed",
    "blocked",
    "abandoned",
    "awaiting_approval",
    "verified_complete",
    "rejected",
}


@dataclass(frozen=True)
class GoalEvent:
    id: str
    goal_id: str
    event_type: str
    status: str
    run_id: str
    trace_id: str
    evidence_json: str
    created_at: float


class GoalStore:
    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = db_paths(home).goals
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            ensure_schema_version(conn, "goals", GOAL_STORE_SCHEMA_VERSION)
            conn.execute(GOALS_TABLE.ddl)
            assert_schema_exact(conn, GOALS_TABLE)
            conn.execute(GOAL_EVENTS_TABLE.ddl)
            assert_schema_exact(conn, GOAL_EVENTS_TABLE)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status, updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_run ON goals(run_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_goal_events_goal ON goal_events(goal_id, created_at)"
            )

    def create(
        self,
        *,
        objective: str,
        workspace: str,
        source: str = "",
        peer_id: str = "",
        sender_id: str = "",
        session_id: str = "",
        run_id: str = "",
        trace_id: str = "",
        evidence: dict[str, Any] | None = None,
        stop_condition: str = "",
        timeout: float = 0.0,
        max_retries: int = 0,
    ) -> Goal:
        now = time.time()
        goal = Goal(
            id=uuid.uuid4().hex,
            objective=objective,
            status=GOAL_STATUS_ACTIVE,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            session_id=session_id,
            workspace=_require_workspace(workspace),
            run_id=run_id,
            trace_id=trace_id,
            evidence_json=json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
            blocked_reason="",
            stop_condition=stop_condition,
            timeout=timeout,
            max_retries=max_retries,
            created_at=now,
            updated_at=now,
            completed_at=0.0,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO goals(
                    id, objective, status, source, peer_id, sender_id, session_id,
                    workspace, run_id, trace_id, evidence_json, blocked_reason,
                    stop_condition, timeout, max_retries,
                    created_at, updated_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    goal.id,
                    goal.objective,
                    goal.status,
                    goal.source,
                    goal.peer_id,
                    goal.sender_id,
                    goal.session_id,
                    goal.workspace,
                    goal.run_id,
                    goal.trace_id,
                    goal.evidence_json,
                    goal.blocked_reason,
                    goal.stop_condition,
                    goal.timeout,
                    goal.max_retries,
                    goal.created_at,
                    goal.updated_at,
                    goal.completed_at,
                ),
            )
        self.record_event(
            goal.id,
            "goal.created",
            status=goal.status,
            run_id=run_id,
            trace_id=trace_id,
            evidence=evidence or {},
        )
        return goal

    def get(self, goal_id: str) -> Goal | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, objective, status, source, peer_id, sender_id, session_id,
                       workspace, run_id, trace_id, evidence_json, blocked_reason,
                       stop_condition, timeout, max_retries,
                       created_at, updated_at, completed_at
                FROM goals WHERE id = ?
                """,
                (goal_id,),
            ).fetchone()
        return Goal(*row) if row else None

    def get_by_run(self, run_id: str) -> Goal | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, objective, status, source, peer_id, sender_id, session_id,
                       workspace, run_id, trace_id, evidence_json, blocked_reason,
                       stop_condition, timeout, max_retries,
                       created_at, updated_at, completed_at
                FROM goals WHERE run_id = ? ORDER BY updated_at DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return Goal(*row) if row else None

    def list(self, *, status: str = "", limit: int = 50) -> list[Goal]:
        if status:
            query = """
                SELECT id, objective, status, source, peer_id, sender_id, session_id,
                       workspace, run_id, trace_id, evidence_json, blocked_reason,
                       stop_condition, timeout, max_retries,
                       created_at, updated_at, completed_at
                FROM goals WHERE status = ? ORDER BY updated_at DESC LIMIT ?
                """
            params: tuple[Any, ...] = (status, limit)
        else:
            query = """
                SELECT id, objective, status, source, peer_id, sender_id, session_id,
                       workspace, run_id, trace_id, evidence_json, blocked_reason,
                       stop_condition, timeout, max_retries,
                       created_at, updated_at, completed_at
                FROM goals ORDER BY updated_at DESC LIMIT ?
                """
            params = (limit,)
        with connect(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [Goal(*row) for row in rows]

    # Events that encode durable constraint state and must survive context
    # compression (principle 12). Pending approvals, denials, rejections, and
    # blocked state cannot be dropped by an LLM summary -- they are reloaded
    # from the store, not trusted to live only inside the model context window.
    _CONSTRAINT_EVENT_STATUSES = frozenset(
        {GOAL_STATUS_AWAITING_APPROVAL, GOAL_STATUS_BLOCKED, GOAL_STATUS_REJECTED}
    )

    async def compact_events(self, goal_id: str, provider, *, threshold: int = 20) -> bool:
        events = self.list_events(goal_id, limit=1000)
        events.sort(key=lambda x: x.created_at)
        if len(events) <= threshold:
            return False

        # Constraint-bearing events are preserved verbatim inside the compaction
        # record. Routine events are summarized. This bounds context growth while
        # guaranteeing durable constraint state survives compression (principle 12).
        preserved_events = [
            {
                "id": e.id,
                "event_type": e.event_type,
                "status": e.status,
                "run_id": e.run_id,
                "trace_id": e.trace_id,
                "evidence_json": e.evidence_json,
                "created_at": e.created_at,
            }
            for e in events
            if e.status in self._CONSTRAINT_EVENT_STATUSES
        ]
        deletable_ids = [
            e.id for e in events if e.status not in self._CONSTRAINT_EVENT_STATUSES
        ]

        from navi.provider import ChatMessage

        lines = []
        for e in events:
            lines.append(f"[{e.created_at}] {e.event_type} {e.status} {e.evidence_json}")

        prompt = (
            "Summarize the following goal events to preserve intent, completed steps, pending approvals, "
            "unresolved questions, and safety constraints. Do not lose any constraints or pending approvals.\n\n"
            + "\n".join(lines)
        )
        summary = await provider.complete_for("planner", [ChatMessage("user", prompt)])

        event = GoalEvent(
            id=uuid.uuid4().hex,
            goal_id=goal_id,
            event_type="goal.compaction",
            status="active",
            run_id="",
            trace_id="",
            evidence_json=json.dumps(
                {"summary": summary, "preserved_events": preserved_events},
                ensure_ascii=False,
                sort_keys=True,
            ),
            created_at=time.time(),
        )

        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO goal_events(id, goal_id, event_type, status, run_id, trace_id, evidence_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.goal_id,
                    event.event_type,
                    event.status,
                    event.run_id,
                    event.trace_id,
                    event.evidence_json,
                    event.created_at,
                ),
            )
            # Delete only the routine (non-constraint) events; constraint-bearing
            # events remain the structured source of truth alongside the summary.
            for event_id in deletable_ids:
                conn.execute("DELETE FROM goal_events WHERE id = ?", (event_id,))

        return True

    def list_events(self, goal_id: str, *, limit: int = 100) -> list[GoalEvent]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, goal_id, event_type, status, run_id, trace_id, evidence_json, created_at
                FROM goal_events WHERE goal_id = ? ORDER BY created_at ASC LIMIT ?
                """,
                (goal_id, limit),
            ).fetchall()
        return [GoalEvent(*row) for row in rows]

    def attach_trace(
        self,
        goal_id: str,
        *,
        trace_id: str,
        session_id: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> Goal | None:
        goal = self.get(goal_id)
        if goal is None:
            return None
        merged_evidence = _merge_evidence(goal.evidence_json, evidence)
        now = time.time()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE goals SET trace_id = ?, session_id = ?, evidence_json = ?, updated_at = ? WHERE id = ?
                """,
                (
                    trace_id or goal.trace_id,
                    session_id or goal.session_id,
                    json.dumps(merged_evidence, ensure_ascii=False, sort_keys=True),
                    now,
                    goal_id,
                ),
            )
        self.record_event(
            goal_id,
            "goal.trace_attached",
            status=goal.status,
            run_id=goal.run_id,
            trace_id=trace_id,
            evidence=evidence or {},
        )
        return self.get(goal_id)

    def update_status(
        self,
        goal_id: str,
        status: str,
        *,
        blocked_reason: str = "",
        evidence: dict[str, Any] | None = None,
        event_type: str = "goal.status",
    ) -> Goal | None:
        if status not in GOAL_STATUSES:
            raise ValueError(f"Invalid goal status: {status}")
        goal = self.get(goal_id)
        if goal is None:
            return None
        merged_evidence = _merge_evidence(goal.evidence_json, evidence)
        now = time.time()
        completed_at = (
            now
            if status in {GOAL_STATUS_VERIFIED_COMPLETE, GOAL_STATUS_BLOCKED, GOAL_STATUS_REJECTED}
            else 0.0
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE goals
                SET status = ?, blocked_reason = ?, evidence_json = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    blocked_reason,
                    json.dumps(merged_evidence, ensure_ascii=False, sort_keys=True),
                    now,
                    completed_at,
                    goal_id,
                ),
            )
        self.record_event(
            goal_id,
            event_type,
            status=status,
            run_id=goal.run_id,
            trace_id=goal.trace_id,
            evidence=evidence or {},
        )
        return self.get(goal_id)

    def stop_condition_facts(self, goal_id: str) -> dict[str, Any]:
        """Return structured facts when a long-running goal hits a declared
        state-based stop boundary.

        Principle 17: long-running goals need explicit stop conditions, so a goal
        is not retried or kept active forever. This is a boundary rule over durable
        state (wall-clock age, recorded retry events) only -- it makes no semantic
        judgement about whether the objective is "done"; that stays with the agent.
        """
        goal = self.get(goal_id)
        if goal is None:
            return {}
        if goal.status != GOAL_STATUS_ACTIVE:
            return {}
        if goal.timeout > 0:
            age = time.time() - goal.created_at
            if age >= goal.timeout:
                return {
                    "reason": "timeout_reached",
                    "age_seconds": int(age),
                    "timeout_seconds": int(goal.timeout),
                }
        if goal.max_retries > 0:
            retries = sum(
                1
                for event in self.list_events(goal_id, limit=1000)
                if event.event_type == "goal.run_status"
                and event.status in {GOAL_STATUS_BLOCKED, "failed"}
            )
            if retries >= goal.max_retries:
                return {
                    "reason": "retry_ceiling_reached",
                    "retry_count": retries,
                    "max_retries": goal.max_retries,
                }
        return {}

    def stop_condition_reached(self, goal_id: str) -> str:
        facts = self.stop_condition_facts(goal_id)
        if not facts:
            return ""
        return str(facts.get("reason") or "")

    def update_for_run(self, run: Run, *, evidence: dict[str, Any] | None = None) -> Goal | None:
        goal = self.get_by_run(run.id)
        if goal is None:
            return None
        evidence = evidence or {"run_id": run.id, "run_status": run.status}
        status = _goal_status_for_run(run, evidence=evidence)
        reason = ""
        if status == GOAL_STATUS_BLOCKED:
            if run.status == RUN_STATUS_COMPLETED and not run.error:
                reason = "critic_gate_evidence_missing"
            else:
                reason = "run_blocked"
            if run.error:
                evidence = {**evidence, "run_error": run.error}
        return self.update_status(
            goal.id,
            status=status,
            blocked_reason=reason,
            evidence=evidence,
            event_type="goal.run_status",
        )

    def record_event(
        self,
        goal_id: str,
        event_type: str,
        *,
        status: str,
        run_id: str = "",
        trace_id: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> GoalEvent:
        event = GoalEvent(
            id=uuid.uuid4().hex,
            goal_id=goal_id,
            event_type=event_type,
            status=status,
            run_id=run_id,
            trace_id=trace_id,
            evidence_json=json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
            created_at=time.time(),
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO goal_events(id, goal_id, event_type, status, run_id, trace_id, evidence_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.goal_id,
                    event.event_type,
                    event.status,
                    event.run_id,
                    event.trace_id,
                    event.evidence_json,
                    event.created_at,
                ),
            )
        return event


def _goal_status_for_run(run: Run, *, evidence: dict[str, Any] | None = None) -> str:
    if run.status == RUN_STATUS_COMPLETED:
        return GOAL_STATUS_VERIFIED_COMPLETE
    if run.status == RUN_STATUS_FAILED:
        return GOAL_STATUS_REJECTED
    return GOAL_STATUS_ACTIVE


def _merge_evidence(existing_json: str, evidence: dict[str, Any] | None) -> dict[str, Any]:
    try:
        existing = json.loads(existing_json or "{}")
    except json.JSONDecodeError:
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    if evidence:
        return {**existing, **evidence}
    return existing


GOALS_TABLE = Table(
    "goals",
    [
        Column("id", "TEXT", primary_key=True),
        Column("objective", "TEXT", nullable=False),
        Column("status", "TEXT", nullable=False),
        Column("source", "TEXT", nullable=False),
        Column("peer_id", "TEXT", nullable=False),
        Column("sender_id", "TEXT", nullable=False),
        Column("session_id", "TEXT", nullable=False),
        Column("workspace", "TEXT", nullable=False),
        Column("run_id", "TEXT", nullable=False),
        Column("trace_id", "TEXT", nullable=False),
        Column("evidence_json", "TEXT", nullable=False),
        Column("blocked_reason", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
        Column("updated_at", "REAL", nullable=False),
        Column("completed_at", "REAL", nullable=False),
        Column("stop_condition", "TEXT", nullable=False),
        Column("timeout", "REAL", nullable=False),
        Column("max_retries", "INTEGER", nullable=False),
    ],
)

GOAL_EVENTS_TABLE = Table(
    "goal_events",
    [
        Column("id", "TEXT", primary_key=True),
        Column("goal_id", "TEXT", nullable=False),
        Column("event_type", "TEXT", nullable=False),
        Column("status", "TEXT", nullable=False),
        Column("run_id", "TEXT", nullable=False),
        Column("trace_id", "TEXT", nullable=False),
        Column("evidence_json", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
    ],
)
