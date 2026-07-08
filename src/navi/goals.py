from __future__ import annotations
from .lifecycle import Phase, Governance, Acceptance, Resolution

import json
import typing
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect, check_schema_version, write_schema_version
from .paths import db_paths
from .runs import Run
from .schema import Column, Table, assert_schema_exact

GOAL_STORE_SCHEMA_VERSION = 3



def _require_workspace(workspace: str) -> str:
    value = workspace.strip()
    if not value:
        raise ValueError("workspace is required")
    return value


@dataclass(frozen=True)
class Goal:
    id: str
    objective: str
    phase: str
    governance: str
    acceptance: str
    resolution: str
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
    # Gap F: persistent task tree fields.
    parent_goal_id: str = ""
    task_status: str = "in_progress"




@dataclass(frozen=True)
class GoalEvent:
    id: str
    goal_id: str
    event_type: str
    phase: str
    governance: str
    acceptance: str
    resolution: str
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
            check_schema_version(conn, "goals", GOAL_STORE_SCHEMA_VERSION)
            conn.execute(GOALS_TABLE.ddl)
            assert_schema_exact(conn, GOALS_TABLE)
            conn.execute(GOAL_EVENTS_TABLE.ddl)
            assert_schema_exact(conn, GOAL_EVENTS_TABLE)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(phase, updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_run ON goals(run_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_goal_events_goal ON goal_events(goal_id, created_at)"
            )
            write_schema_version(conn, "goals", GOAL_STORE_SCHEMA_VERSION)

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
        parent_goal_id: str = "",
        task_status: str = "in_progress",
    ) -> Goal:
        now = time.time()
        goal = Goal(
            id=uuid.uuid4().hex,
            objective=objective,
            phase=Phase.RUNNING, governance=Governance.APPROVED, acceptance=Acceptance.NONE, resolution=Resolution.NONE,
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
            parent_goal_id=parent_goal_id,
            task_status=task_status,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO goals(
                    id, objective, phase, governance, acceptance, resolution, source, peer_id, sender_id, session_id,
                    workspace, run_id, trace_id, evidence_json, blocked_reason,
                    stop_condition, timeout, max_retries,
                    created_at, updated_at, completed_at,
                    parent_goal_id, task_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    goal.id,
                    goal.objective,
                    goal.phase,
                    goal.governance,
                    goal.acceptance,
                    goal.resolution,
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
                    goal.parent_goal_id,
                    goal.task_status,
                ),
            )
        self.record_event(
            goal.id,
            "goal.created",
            phase=goal.phase, governance=goal.governance, acceptance=goal.acceptance, resolution=goal.resolution,
            run_id=run_id,
            trace_id=trace_id,
            evidence=evidence or {},
        )
        return goal

    def get(self, goal_id: str) -> Goal | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, objective, phase, governance, acceptance, resolution, source, peer_id, sender_id, session_id,
                       workspace, run_id, trace_id, evidence_json, blocked_reason,
                       stop_condition, timeout, max_retries,
                       created_at, updated_at, completed_at,
                       parent_goal_id, task_status
                FROM goals WHERE id = ?
                """,
                (goal_id,),
            ).fetchone()
        return Goal(*row) if row else None

    def get_by_run(self, run_id: str) -> Goal | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, objective, phase, governance, acceptance, resolution, source, peer_id, sender_id, session_id,
                       workspace, run_id, trace_id, evidence_json, blocked_reason,
                       stop_condition, timeout, max_retries,
                       created_at, updated_at, completed_at,
                       parent_goal_id, task_status
                FROM goals WHERE run_id = ? ORDER BY updated_at DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return Goal(*row) if row else None

    def list(self, *, phase: str = "", limit: int = 50) -> typing.List[Goal]:
        if phase:
            query = """
                SELECT id, objective, phase, governance, acceptance, resolution, source, peer_id, sender_id, session_id,
                       workspace, run_id, trace_id, evidence_json, blocked_reason,
                       stop_condition, timeout, max_retries,
                       created_at, updated_at, completed_at,
                       parent_goal_id, task_status
                FROM goals WHERE phase = ? ORDER BY updated_at DESC LIMIT ?
                """
            params: tuple[Any, ...] = (phase, limit)
        else:
            query = """
                SELECT id, objective, phase, governance, acceptance, resolution, source, peer_id, sender_id, session_id,
                       workspace, run_id, trace_id, evidence_json, blocked_reason,
                       stop_condition, timeout, max_retries,
                       created_at, updated_at, completed_at,
                       parent_goal_id, task_status
                FROM goals ORDER BY updated_at DESC LIMIT ?
                """
            params = (limit,)
        with connect(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [Goal(*row) for row in rows]

    def list_children(self, parent_goal_id: str, *, limit: int = 50) -> typing.List[Goal]:
        """List child goals of *parent_goal_id* (Gap F task tree)."""
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, objective, phase, governance, acceptance, resolution, source, peer_id, sender_id, session_id,
                       workspace, run_id, trace_id, evidence_json, blocked_reason,
                       stop_condition, timeout, max_retries,
                       created_at, updated_at, completed_at,
                       parent_goal_id, task_status
                FROM goals WHERE parent_goal_id = ? ORDER BY created_at ASC LIMIT ?
                """,
                (parent_goal_id, limit),
            ).fetchall()
        return [Goal(*row) for row in rows]

    def update_task_status(self, goal_id: str, task_status: str) -> Goal | None:
        """Update the ``task_status`` of a goal (Gap F task tree).

        ``task_status`` is the explicit lifecycle state
        (pending/in_progress/done/blocked) that the model updates as it
        progresses through sub-tasks.
        """
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE goals SET task_status = ?, updated_at = ? WHERE id = ?",
                (task_status, time.time(), goal_id),
            )
        return self.get(goal_id)

    # Events that encode durable constraint state and must survive context
    # compression (principle 12). Pending approvals, denials, rejections, and
    # blocked state cannot be dropped by an LLM summary -- they are reloaded
    # from the store, not trusted to live only inside the model context window.
    def _is_constraint_event(self, e: GoalEvent) -> bool:
        return e.governance == Governance.AWAITING_APPROVAL or e.resolution in {Resolution.BLOCKED, Resolution.FAILED}

    async def compact_events(self, goal_id: str, runtime, *, threshold: int = 20) -> bool:
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
                "phase": e.phase, "governance": e.governance, "acceptance": e.acceptance, "resolution": e.resolution,
                "run_id": e.run_id,
                "trace_id": e.trace_id,
                "evidence_json": e.evidence_json,
                "created_at": e.created_at,
            }
            for e in events
            if self._is_constraint_event(e)
        ]
        deletable_ids = [
            e.id for e in events if not self._is_constraint_event(e)
        ]

        from navi.provider import ChatMessage

        lines = []
        for e in events:
            lines.append(f"[{e.created_at}] {e.event_type} {e.phase} {e.evidence_json}")

        prompt = (
            "Summarize the following goal events to preserve intent, completed steps, pending approvals, "
            "unresolved questions, and safety constraints. Do not lose any constraints or pending approvals.\n\n"
            + "\n".join(lines)
        )
        summary = await runtime.complete([ChatMessage("user", prompt)], role="planner")

        event = GoalEvent(
            id=uuid.uuid4().hex,
            goal_id=goal_id,
            event_type="goal.compaction",
            phase=Phase.RUNNING, governance=Governance.NONE, acceptance=Acceptance.NONE, resolution=Resolution.NONE,
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
                INSERT INTO goal_events(id, goal_id, event_type, phase, governance, acceptance, resolution, run_id, trace_id, evidence_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.goal_id,
                    event.event_type,
                    event.phase,
                    event.governance,
                    event.acceptance,
                    event.resolution,
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

    def list_events(self, goal_id: str, *, limit: int = 100) -> typing.List[GoalEvent]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, goal_id, event_type, phase, governance, acceptance, resolution, run_id, trace_id, evidence_json, created_at
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
            phase=goal.phase, governance=goal.governance, acceptance=goal.acceptance, resolution=goal.resolution,
            run_id=goal.run_id,
            trace_id=trace_id,
            evidence=evidence or {},
        )
        return self.get(goal_id)

    def update_state(
        self,
        goal_id: str,
        *,
        phase: str | None = None,
        governance: str | None = None,
        acceptance: str | None = None,
        resolution: str | None = None,
        blocked_reason: str = "",
        evidence: dict[str, Any] | None = None,
        event_type: str = "goal.state",
    ) -> Goal | None:
        goal = self.get(goal_id)
        if goal is None:
            return None
        next_phase = goal.phase if phase is None else phase
        next_governance = goal.governance if governance is None else governance
        next_acceptance = goal.acceptance if acceptance is None else acceptance
        next_resolution = goal.resolution if resolution is None else resolution
        merged_evidence = _merge_evidence(goal.evidence_json, evidence)
        now = time.time()
        completed_at = (
            now
            if next_phase == Phase.ENDED
            else 0.0
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE goals
                SET phase = ?, governance = ?, acceptance = ?, resolution = ?, blocked_reason = ?, evidence_json = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    next_phase,
                    next_governance,
                    next_acceptance,
                    next_resolution,
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
            phase=next_phase,
            governance=next_governance,
            acceptance=next_acceptance,
            resolution=next_resolution,
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
        if goal.phase != Phase.RUNNING:
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
                if event.event_type == "goal.run_state"
                and event.resolution in {Resolution.BLOCKED, Resolution.FAILED}
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
        evidence = evidence or {
            "run_id": run.id,
            "run_phase": run.phase,
            "run_governance": run.governance,
            "run_acceptance": run.acceptance,
            "run_resolution": run.resolution,
        }
        phase, governance, acceptance, resolution = _goal_state_for_run(run, evidence=evidence)
        reason = ""
        if resolution == Resolution.BLOCKED:
            if run.phase == Phase.ENDED and run.resolution == Resolution.SUCCESS and not run.error:
                reason = "critic_gate_evidence_missing"
            else:
                reason = "run_blocked"
            if run.error:
                evidence = {**evidence, "run_error": run.error}
        return self.update_state(
            goal.id,
            phase=phase, governance=governance, acceptance=acceptance, resolution=resolution,
            blocked_reason=reason,
            evidence=evidence,
            event_type="goal.run_state",
        )

    def record_event(
        self,
        goal_id: str,
        event_type: str,
        *,
        phase: str,
        governance: str,
        acceptance: str,
        resolution: str,
        run_id: str = "",
        trace_id: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> GoalEvent:
        event = GoalEvent(
            id=uuid.uuid4().hex,
            goal_id=goal_id,
            event_type=event_type,
            phase=phase, governance=governance, acceptance=acceptance, resolution=resolution,
            run_id=run_id,
            trace_id=trace_id,
            evidence_json=json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
            created_at=time.time(),
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO goal_events(id, goal_id, event_type, phase, governance, acceptance, resolution, run_id, trace_id, evidence_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.goal_id,
                    event.event_type,
                    event.phase,
                    event.governance,
                    event.acceptance,
                    event.resolution,
                    event.run_id,
                    event.trace_id,
                    event.evidence_json,
                    event.created_at,
                ),
            )
        return event


def _goal_state_for_run(run: Run, *, evidence: dict[str, Any] | None = None) -> tuple[str, str, str, str]:
    if run.phase == Phase.ENDED and run.resolution == Resolution.SUCCESS:
        return (Phase.ENDED, Governance.NONE, Acceptance.ACCEPTED, Resolution.SUCCESS)
    if run.phase == Phase.ENDED:
        acceptance = Acceptance.REJECTED if run.resolution == Resolution.FAILED else Acceptance.NONE
        return (Phase.ENDED, Governance.NONE, acceptance, run.resolution)
    if run.governance == Governance.AWAITING_APPROVAL:
        return (Phase.RUNNING, Governance.AWAITING_APPROVAL, Acceptance.NONE, Resolution.NONE)
    if run.resolution == Resolution.BLOCKED:
        return (Phase.RUNNING, run.governance, Acceptance.NONE, Resolution.BLOCKED)
    return (Phase.RUNNING, Governance.APPROVED, Acceptance.NONE, Resolution.NONE)


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
        Column("phase", "TEXT", nullable=False),
        Column("governance", "TEXT", nullable=False),
        Column("acceptance", "TEXT", nullable=False),
        Column("resolution", "TEXT", nullable=False),
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
        # Gap F: persistent task tree. parent_goal_id enables a
        # parent-child hierarchy so the engine can track sub-task
        # decomposition across long-horizon tasks. task_status is the
        # explicit lifecycle state (pending/in_progress/done/blocked)
        # that the model updates as it progresses.
        Column("parent_goal_id", "TEXT", nullable=False),
        Column("task_status", "TEXT", nullable=False),
    ],
)

GOAL_EVENTS_TABLE = Table(
    "goal_events",
    [
        Column("id", "TEXT", primary_key=True),
        Column("goal_id", "TEXT", nullable=False),
        Column("event_type", "TEXT", nullable=False),
        Column("phase", "TEXT", nullable=False),
        Column("governance", "TEXT", nullable=False),
        Column("acceptance", "TEXT", nullable=False),
        Column("resolution", "TEXT", nullable=False),
        Column("run_id", "TEXT", nullable=False),
        Column("trace_id", "TEXT", nullable=False),
        Column("evidence_json", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
    ],
)
