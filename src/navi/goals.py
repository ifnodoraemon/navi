from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect
from .runs import Run

GOAL_STATUS_ACTIVE = "active"
GOAL_STATUS_AWAITING_APPROVAL = "awaiting_approval"
GOAL_STATUS_VERIFIED_COMPLETE = "verified_complete"
GOAL_STATUS_BLOCKED = "blocked"
GOAL_STATUS_REJECTED = "rejected"


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
    created_at: float
    updated_at: float
    completed_at: float


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
        self.db_path = home / "goals.db"
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS goals (
                    id TEXT PRIMARY KEY,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    peer_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    blocked_reason TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL NOT NULL
                )
                """
            )
            _ensure_columns(
                conn,
                "goals",
                {
                    "run_id": "TEXT NOT NULL DEFAULT ''",
                    "trace_id": "TEXT NOT NULL DEFAULT ''",
                    "evidence_json": "TEXT NOT NULL DEFAULT '{}'",
                    "blocked_reason": "TEXT NOT NULL DEFAULT ''",
                    "completed_at": "REAL NOT NULL DEFAULT 0.0",
                },
            )
            _migrate_goals_to_latest_schema(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS goal_events (
                    id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            _ensure_columns(
                conn,
                "goal_events",
                {
                    "run_id": "TEXT NOT NULL DEFAULT ''",
                    "trace_id": "TEXT NOT NULL DEFAULT ''",
                    "evidence_json": "TEXT NOT NULL DEFAULT '{}'",
                },
            )
            _migrate_goal_events_to_latest_schema(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status, updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_run ON goals(run_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_goal_events_goal ON goal_events(goal_id, created_at)")

    def create(
        self,
        *,
        objective: str,
        source: str = "",
        peer_id: str = "",
        sender_id: str = "",
        session_id: str = "",
        workspace: str = "",
        run_id: str = "",
        trace_id: str = "",
        evidence: dict[str, Any] | None = None,
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
            workspace=workspace,
            run_id=run_id,
            trace_id=trace_id,
            evidence_json=json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
            blocked_reason="",
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
                    created_at, updated_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    goal.created_at,
                    goal.updated_at,
                    goal.completed_at,
                ),
            )
        self.record_event(goal.id, "goal.created", status=goal.status, run_id=run_id, trace_id=trace_id, evidence=evidence or {})
        return goal

    def get(self, goal_id: str) -> Goal | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, objective, status, source, peer_id, sender_id, session_id,
                       workspace, run_id, trace_id, evidence_json, blocked_reason,
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
                       created_at, updated_at, completed_at
                FROM goals WHERE status = ? ORDER BY updated_at DESC LIMIT ?
                """
            params: tuple[Any, ...] = (status, limit)
        else:
            query = """
                SELECT id, objective, status, source, peer_id, sender_id, session_id,
                       workspace, run_id, trace_id, evidence_json, blocked_reason,
                       created_at, updated_at, completed_at
                FROM goals ORDER BY updated_at DESC LIMIT ?
                """
            params = (limit,)
        with connect(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [Goal(*row) for row in rows]

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

    def attach_trace(self, goal_id: str, *, trace_id: str, session_id: str = "", evidence: dict[str, Any] | None = None) -> Goal | None:
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
        self.record_event(goal_id, "goal.trace_attached", status=goal.status, run_id=goal.run_id, trace_id=trace_id, evidence=evidence or {})
        return self.get(goal_id)

    def update_status(
        self,
        goal_id: str,
        *,
        status: str,
        blocked_reason: str = "",
        evidence: dict[str, Any] | None = None,
        event_type: str = "goal.status",
    ) -> Goal | None:
        goal = self.get(goal_id)
        if goal is None:
            return None
        merged_evidence = _merge_evidence(goal.evidence_json, evidence)
        now = time.time()
        completed_at = now if status in {GOAL_STATUS_VERIFIED_COMPLETE, GOAL_STATUS_BLOCKED, GOAL_STATUS_REJECTED} else 0.0
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
        self.record_event(goal_id, event_type, status=status, run_id=goal.run_id, trace_id=goal.trace_id, evidence=evidence or {})
        return self.get(goal_id)

    def update_for_run(self, run: Run, *, evidence: dict[str, Any] | None = None) -> Goal | None:
        goal = self.get_by_run(run.id)
        if goal is None:
            return None
        evidence = evidence or {"run_id": run.id, "run_status": run.status}
        status = _goal_status_for_run(run, evidence=evidence)
        reason = run.error if status == GOAL_STATUS_BLOCKED else ""
        if run.status == "completed" and status == GOAL_STATUS_BLOCKED and not reason:
            reason = "critic gate evidence missing or failed"
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
    if run.status == "completed":
        return GOAL_STATUS_VERIFIED_COMPLETE if _critic_passed(evidence or {}) else GOAL_STATUS_BLOCKED
    if run.status == "awaiting_approval":
        return GOAL_STATUS_AWAITING_APPROVAL
    if run.status == "rejected":
        return GOAL_STATUS_REJECTED
    if run.status in {"failed", "blocked"}:
        return GOAL_STATUS_BLOCKED
    return GOAL_STATUS_ACTIVE


def _critic_passed(evidence: dict[str, Any]) -> bool:
    critic = evidence.get("critic")
    if isinstance(critic, dict):
        return critic.get("passed") is True
    return False


def _merge_evidence(existing_json: str, evidence: dict[str, Any] | None) -> dict[str, Any]:
    try:
        existing = json.loads(existing_json or "{}")
    except json.JSONDecodeError:
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    if evidence:
        existing.update(evidence)
    return existing


def _ensure_columns(conn, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _table_columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _expr_for_column(columns: set[str], name: str, default: str) -> str:
    if name == "run_id" and "task_id" in columns:
        return "COALESCE(NULLIF(run_id, ''), task_id, '')" if "run_id" in columns else "COALESCE(task_id, '')"
    if name in columns:
        return name
    return default


def _needs_rebuild(conn, table: str, expected: list[str]) -> bool:
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return columns != expected


def _migrate_goals_to_latest_schema(conn) -> None:
    expected = [
        "id",
        "objective",
        "status",
        "source",
        "peer_id",
        "sender_id",
        "session_id",
        "workspace",
        "run_id",
        "trace_id",
        "evidence_json",
        "blocked_reason",
        "created_at",
        "updated_at",
        "completed_at",
    ]
    if not _needs_rebuild(conn, "goals", expected):
        return
    columns = _table_columns(conn, "goals")
    conn.execute(
        """
        CREATE TABLE goals_latest (
            id TEXT PRIMARY KEY,
            objective TEXT NOT NULL,
            status TEXT NOT NULL,
            source TEXT NOT NULL,
            peer_id TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            workspace TEXT NOT NULL,
            run_id TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            blocked_reason TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL NOT NULL
        )
        """
    )
    select_exprs = [
        _expr_for_column(columns, "id", "''"),
        _expr_for_column(columns, "objective", "''"),
        _expr_for_column(columns, "status", f"'{GOAL_STATUS_ACTIVE}'"),
        _expr_for_column(columns, "source", "''"),
        _expr_for_column(columns, "peer_id", "''"),
        _expr_for_column(columns, "sender_id", "''"),
        _expr_for_column(columns, "session_id", "''"),
        _expr_for_column(columns, "workspace", "''"),
        _expr_for_column(columns, "run_id", "''"),
        _expr_for_column(columns, "trace_id", "''"),
        _expr_for_column(columns, "evidence_json", "'{}'"),
        _expr_for_column(columns, "blocked_reason", "''"),
        _expr_for_column(columns, "created_at", "0.0"),
        _expr_for_column(columns, "updated_at", "0.0"),
        _expr_for_column(columns, "completed_at", "0.0"),
    ]
    conn.execute(
        f"""
        INSERT INTO goals_latest({", ".join(expected)})
        SELECT {", ".join(select_exprs)} FROM goals
        """
    )
    conn.execute("DROP TABLE goals")
    conn.execute("ALTER TABLE goals_latest RENAME TO goals")


def _migrate_goal_events_to_latest_schema(conn) -> None:
    expected = [
        "id",
        "goal_id",
        "event_type",
        "status",
        "run_id",
        "trace_id",
        "evidence_json",
        "created_at",
    ]
    if not _needs_rebuild(conn, "goal_events", expected):
        return
    columns = _table_columns(conn, "goal_events")
    conn.execute(
        """
        CREATE TABLE goal_events_latest (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            status TEXT NOT NULL,
            run_id TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    select_exprs = [
        _expr_for_column(columns, "id", "''"),
        _expr_for_column(columns, "goal_id", "''"),
        _expr_for_column(columns, "event_type", "''"),
        _expr_for_column(columns, "status", "''"),
        _expr_for_column(columns, "run_id", "''"),
        _expr_for_column(columns, "trace_id", "''"),
        _expr_for_column(columns, "evidence_json", "'{}'"),
        _expr_for_column(columns, "created_at", "0.0"),
    ]
    conn.execute(
        f"""
        INSERT INTO goal_events_latest({", ".join(expected)})
        SELECT {", ".join(select_exprs)} FROM goal_events
        """
    )
    conn.execute("DROP TABLE goal_events")
    conn.execute("ALTER TABLE goal_events_latest RENAME TO goal_events")
