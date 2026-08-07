from __future__ import annotations
import builtins
from .lifecycle import Phase, Governance, Acceptance, Resolution

import json
import typing
import time
import uuid
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from .db import connect, check_schema_version, write_schema_version
from .delivery_outbox import DeliveryEnvelope, DeliveryItem, DeliveryOutboxStore
from .paths import db_paths
from .persistence_scope import append_actor_scope
from .prompt_os import assemble_goal_event_compaction_messages
from .runs import Run
from .schema import Column, Table, assert_schema_exact

GOAL_STORE_SCHEMA_VERSION = 5


class ChildAdmissionConflict(ValueError):
    """Raised when a child goal cannot be admitted under the parent limit."""


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
    # Persistent task tree fields.
    parent_goal_id: str = ""
    task_status: str = "in_progress"
    # Cron / recurring fields
    cron_schedule: str = ""
    next_run_at: float = 0.0


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


@dataclass(frozen=True)
class GoalDeliveryOutboxItem:
    id: str
    goal_id: str
    run_id: str
    channel: str
    source: str
    peer_id: str
    sender_id: str
    trace_id: str
    body: str
    body_provenance: str
    status: str
    attempts: int
    error: str
    delivery_id: str
    created_at: float
    updated_at: float
    sent_at: float


GOAL_SELECT_LIST = ", ".join(field.name for field in fields(Goal))
GOAL_DELIVERY_OUTBOX_SELECT_LIST = ", ".join(field.name for field in fields(GoalDeliveryOutboxItem))


class GoalStore:
    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = db_paths(home).goals
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.execute(GOALS_TABLE.ddl)
            check_schema_version(conn, "goals", GOAL_STORE_SCHEMA_VERSION)
            assert_schema_exact(conn, GOALS_TABLE)
            conn.execute(GOAL_EVENTS_TABLE.ddl)
            assert_schema_exact(conn, GOAL_EVENTS_TABLE)
            conn.execute(GOAL_DELIVERY_OUTBOX_TABLE.ddl)
            assert_schema_exact(conn, GOAL_DELIVERY_OUTBOX_TABLE)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(phase, updated_at)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_goals_actor_scope "
                "ON goals(source, peer_id, sender_id, workspace, updated_at)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_goals_run ON goals(run_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_goal_events_goal ON goal_events(goal_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_goal_delivery_outbox_pending "
                "ON goal_delivery_outbox(status, channel, created_at)"
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
        cron_schedule: str = "",
        next_run_at: float = 0.0,
        child_active_limit: int = 0,
    ) -> Goal:
        now = time.time()
        goal = Goal(
            id=uuid.uuid4().hex,
            objective=objective,
            phase=Phase.RUNNING,
            governance=Governance.APPROVED,
            acceptance=Acceptance.NONE,
            resolution=Resolution.NONE,
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
            cron_schedule=cron_schedule,
            next_run_at=next_run_at,
        )
        with connect(self.db_path) as conn:
            if goal.parent_goal_id and child_active_limit > 0:
                conn.execute("BEGIN IMMEDIATE")
                active = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM goals
                    WHERE parent_goal_id = ?
                      AND phase IN (?, ?, ?)
                    """,
                    (goal.parent_goal_id, Phase.PENDING, Phase.RUNNING, Phase.PAUSED),
                ).fetchone()
                active_count = int(active[0]) if active else 0
                if active_count >= child_active_limit:
                    raise ChildAdmissionConflict(
                        "agent.control(operation=spawn) allows at most "
                        f"{child_active_limit} active children per parent."
                    )
            conn.execute(
                """
                INSERT INTO goals(
                    id, objective, phase, governance, acceptance, resolution, source, peer_id, sender_id, session_id,
                    workspace, run_id, trace_id, evidence_json, blocked_reason,
                    stop_condition, timeout, max_retries,
                    created_at, updated_at, completed_at,
                    parent_goal_id, task_status, cron_schedule, next_run_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    goal.cron_schedule,
                    goal.next_run_at,
                ),
            )
        self.record_event(
            goal.id,
            "goal.created",
            phase=goal.phase,
            governance=goal.governance,
            acceptance=goal.acceptance,
            resolution=goal.resolution,
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
                       parent_goal_id, task_status, cron_schedule, next_run_at
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
                       parent_goal_id, task_status, cron_schedule, next_run_at
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
                       parent_goal_id, task_status, cron_schedule, next_run_at
                FROM goals WHERE phase = ? ORDER BY updated_at DESC LIMIT ?
                """
            params: tuple[Any, ...] = (phase, limit)
        else:
            query = """
                SELECT id, objective, phase, governance, acceptance, resolution, source, peer_id, sender_id, session_id,
                       workspace, run_id, trace_id, evidence_json, blocked_reason,
                       stop_condition, timeout, max_retries,
                       created_at, updated_at, completed_at,
                       parent_goal_id, task_status, cron_schedule, next_run_at
                FROM goals ORDER BY updated_at DESC LIMIT ?
                """
            params = (limit,)
        with connect(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [Goal(*row) for row in rows]

    def list_scoped(
        self,
        *,
        source: str = "",
        peer_id: str = "",
        sender_id: str = "",
        session_id: str = "",
        workspace: str = "",
        phases: tuple[str, ...] = (),
        governance: tuple[str, ...] = (),
        task_status: tuple[str, ...] = (),
        resolution: tuple[str, ...] = (),
        cron: bool | None = None,
        child: bool | None = None,
        limit: int | None = 50,
    ) -> typing.List[Goal]:
        """List goals after applying actor and workspace scope in SQL."""
        clauses: list[str] = []
        params: list[Any] = []
        append_actor_scope(
            clauses,
            params,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            workspace=workspace,
        )
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if phases:
            placeholders = ", ".join("?" for _ in phases)
            clauses.append(f"phase IN ({placeholders})")
            params.extend(phases)
        if governance:
            placeholders = ", ".join("?" for _ in governance)
            clauses.append(f"governance IN ({placeholders})")
            params.extend(governance)
        if task_status:
            placeholders = ", ".join("?" for _ in task_status)
            clauses.append(f"task_status IN ({placeholders})")
            params.extend(task_status)
        if resolution:
            placeholders = ", ".join("?" for _ in resolution)
            clauses.append(f"resolution IN ({placeholders})")
            params.extend(resolution)
        if cron is not None:
            clauses.append("cron_schedule != ''" if cron else "cron_schedule = ''")
        if child is not None:
            clauses.append("parent_goal_id != ''" if child else "parent_goal_id = ''")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_clause = ""
        if limit is not None:
            limit_clause = " LIMIT ?"
            params.append(max(1, int(limit)))
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT {GOAL_SELECT_LIST}
                FROM goals{where}
                ORDER BY updated_at DESC{limit_clause}
                """,
                params,
            ).fetchall()
        return [Goal(*row) for row in rows]

    def count_scoped(
        self,
        *,
        source: str = "",
        peer_id: str = "",
        sender_id: str = "",
        session_id: str = "",
        workspace: str = "",
        phases: tuple[str, ...] = (),
        governance: tuple[str, ...] = (),
        task_status: tuple[str, ...] = (),
        resolution: tuple[str, ...] = (),
        cron: bool | None = None,
        child: bool | None = None,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        append_actor_scope(
            clauses,
            params,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            workspace=workspace,
        )
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if phases:
            placeholders = ", ".join("?" for _ in phases)
            clauses.append(f"phase IN ({placeholders})")
            params.extend(phases)
        if governance:
            placeholders = ", ".join("?" for _ in governance)
            clauses.append(f"governance IN ({placeholders})")
            params.extend(governance)
        if task_status:
            placeholders = ", ".join("?" for _ in task_status)
            clauses.append(f"task_status IN ({placeholders})")
            params.extend(task_status)
        if resolution:
            placeholders = ", ".join("?" for _ in resolution)
            clauses.append(f"resolution IN ({placeholders})")
            params.extend(resolution)
        if cron is not None:
            clauses.append("cron_schedule != ''" if cron else "cron_schedule = ''")
        if child is not None:
            clauses.append("parent_goal_id != ''" if child else "parent_goal_id = ''")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with connect(self.db_path) as conn:
            row = conn.execute(f"SELECT COUNT(*) FROM goals{where}", params).fetchone()
        return int(row[0]) if row else 0

    def list_children(
        self,
        parent_goal_id: str,
        *,
        limit: int = 50,
        newest: bool = False,
        created_after: float = 0.0,
        created_before: float = 0.0,
        resolutions: tuple[str, ...] = (),
    ) -> typing.List[Goal]:
        """List child goals of *parent_goal_id*."""
        order = "DESC" if newest else "ASC"
        clauses = ["parent_goal_id = ?"]
        params: list[Any] = [parent_goal_id]
        if created_after > 0:
            clauses.append("created_at >= ?")
            params.append(float(created_after))
        if created_before > 0:
            clauses.append("created_at <= ?")
            params.append(float(created_before))
        if resolutions:
            placeholders = ", ".join("?" for _ in resolutions)
            clauses.append(f"resolution IN ({placeholders})")
            params.extend(resolutions)
        params.append(max(1, int(limit)))
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT id, objective, phase, governance, acceptance, resolution, source, peer_id, sender_id, session_id,
                       workspace, run_id, trace_id, evidence_json, blocked_reason,
                       stop_condition, timeout, max_retries,
                       created_at, updated_at, completed_at,
                       parent_goal_id, task_status, cron_schedule, next_run_at
                FROM goals WHERE {" AND ".join(clauses)}
                ORDER BY created_at {order} LIMIT ?
                """,
                params,
            ).fetchall()
        goals = [Goal(*row) for row in rows]
        return list(reversed(goals)) if newest else goals

    def count_children(
        self,
        parent_goal_id: str,
        *,
        phases: tuple[str, ...] = (),
    ) -> int:
        clauses = ["parent_goal_id = ?"]
        params: list[Any] = [parent_goal_id]
        if phases:
            placeholders = ", ".join("?" for _ in phases)
            clauses.append(f"phase IN ({placeholders})")
            params.extend(phases)
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM goals WHERE {' AND '.join(clauses)}",
                params,
            ).fetchone()
        return int(row[0]) if row else 0

    def update_task_status(self, goal_id: str, task_status: str) -> Goal | None:
        """Update the ``task_status`` of a goal.

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
    # compression. Pending approvals, denials, rejections, and
    # blocked state cannot be dropped by an LLM summary -- they are reloaded
    # from the store, not trusted to live only inside the model context window.
    def _is_constraint_event(self, e: GoalEvent) -> bool:
        return e.governance == Governance.AWAITING_APPROVAL or e.resolution in {
            Resolution.BLOCKED,
            Resolution.FAILED,
        }

    async def compact_events(self, goal_id: str, runtime, *, threshold: int = 20) -> bool:
        events = self.list_events(goal_id, limit=None)
        events.sort(key=lambda x: x.created_at)
        if len(events) <= threshold:
            return False

        # Constraint-bearing events are preserved verbatim inside the compaction
        # record. Routine events are summarized. This bounds context growth while
        # guaranteeing durable constraint state survives compression.
        preserved_events = [
            {
                "id": e.id,
                "event_type": e.event_type,
                "phase": e.phase,
                "governance": e.governance,
                "acceptance": e.acceptance,
                "resolution": e.resolution,
                "run_id": e.run_id,
                "trace_id": e.trace_id,
                "evidence_json": e.evidence_json,
                "created_at": e.created_at,
            }
            for e in events
            if self._is_constraint_event(e)
        ]
        deletable_ids = [e.id for e in events if not self._is_constraint_event(e)]

        lines = []
        for e in events:
            lines.append(f"[{e.created_at}] {e.event_type} {e.phase} {e.evidence_json}")

        summary = await runtime.complete(
            assemble_goal_event_compaction_messages(lines), role="planner"
        )

        event = GoalEvent(
            id=uuid.uuid4().hex,
            goal_id=goal_id,
            event_type="goal.compaction",
            phase=Phase.RUNNING,
            governance=Governance.NONE,
            acceptance=Acceptance.NONE,
            resolution=Resolution.NONE,
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

    def list_events(
        self,
        goal_id: str,
        *,
        limit: int | None = 100,
    ) -> typing.List[GoalEvent]:
        limit_clause = ""
        params: list[Any] = [goal_id]
        if limit is not None:
            limit_clause = " LIMIT ?"
            params.append(max(1, int(limit)))
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT id, goal_id, event_type, phase, governance, acceptance, resolution, run_id, trace_id, evidence_json, created_at
                FROM goal_events WHERE goal_id = ? ORDER BY created_at ASC{limit_clause}
                """,
                params,
            ).fetchall()
        return [GoalEvent(*row) for row in rows]

    def count_events(
        self,
        goal_id: str,
        *,
        event_type: str = "",
        resolutions: tuple[str, ...] = (),
    ) -> int:
        clauses = ["goal_id = ?"]
        params: list[Any] = [goal_id]
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if resolutions:
            placeholders = ", ".join("?" for _ in resolutions)
            clauses.append(f"resolution IN ({placeholders})")
            params.extend(resolutions)
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM goal_events WHERE {' AND '.join(clauses)}",
                params,
            ).fetchone()
        return int(row[0]) if row else 0

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
            phase=goal.phase,
            governance=goal.governance,
            acceptance=goal.acceptance,
            resolution=goal.resolution,
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
        task_status: str | None = None,
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
        next_task_status = goal.task_status if task_status is None else task_status
        merged_evidence = _merge_evidence(goal.evidence_json, evidence)
        now = time.time()
        completed_at = now if next_phase == Phase.ENDED else 0.0
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE goals
                SET phase = ?, governance = ?, acceptance = ?, resolution = ?, task_status = ?,
                    blocked_reason = ?, evidence_json = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    next_phase,
                    next_governance,
                    next_acceptance,
                    next_resolution,
                    next_task_status,
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

        Long-running goals need explicit stop conditions, so a goal
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
            retries = self.count_events(
                goal_id,
                event_type="goal.run_state",
                resolutions=(Resolution.BLOCKED, Resolution.FAILED),
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
        if phase == Phase.ENDED and resolution == Resolution.SUCCESS:
            task_status = "done"
        elif phase == Phase.ENDED or resolution in {
            Resolution.BLOCKED,
            Resolution.FAILED,
            Resolution.CANCELED,
        }:
            task_status = "blocked"
        elif governance == Governance.AWAITING_APPROVAL:
            task_status = "pending"
        else:
            task_status = "in_progress"
        try:
            goal_evidence = json.loads(goal.evidence_json or "{}")
        except json.JSONDecodeError:
            goal_evidence = {}
        if phase == Phase.ENDED and goal_evidence.get("loop_kind") == "turn":
            task_status = "archived"
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
            phase=phase,
            governance=governance,
            acceptance=acceptance,
            resolution=resolution,
            task_status=task_status,
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
            phase=phase,
            governance=governance,
            acceptance=acceptance,
            resolution=resolution,
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

    def record_result_delivery_outbox(
        self,
        *,
        run: Run,
        goal: Goal,
        body: str,
        body_provenance: str,
        channel: str = "",
        trace_id: str = "",
    ) -> DeliveryItem | None:
        """Persist an accepted result through the connector-neutral outbox."""
        text = body.strip()
        surface = channel.strip() or goal.source or run.source
        peer_id = goal.peer_id or run.peer_id
        if not text or not surface or not peer_id:
            return None
        outbox = DeliveryOutboxStore(self.home)
        existing = outbox.latest_for_run(run.id)
        if existing is not None:
            return existing
        transport_context: dict[str, Any] = {"priority": 0}
        if goal.parent_goal_id:
            parent = self.get(goal.parent_goal_id)
            if parent is not None and parent.next_run_at > 0:
                transport_context.update(
                    {
                        "expires_at": parent.next_run_at,
                        "supersession_key": parent.id,
                    }
                )
        items = outbox.enqueue(
            DeliveryEnvelope(
                batch_id=f"goal:{run.id}",
                channel=surface,
                peer_id=peer_id,
                sender_id=goal.sender_id or run.sender_id,
                trace_id=trace_id or goal.trace_id or run.id,
                run_id=run.id,
                goal_id=goal.id,
                text=text,
                body_provenance=body_provenance,
                transport_context=transport_context,
            )
        )
        return items[0] if items else None

    def list_unprojected_sent_delivery_outbox(
        self,
        *,
        channel: str,
        limit: int = 100,
    ) -> builtins.list[DeliveryItem]:
        """Return complete batches whose Goal projection still needs repair."""
        items: list[DeliveryItem] = []
        for _batch_id, batch in DeliveryOutboxStore(self.home).complete_unprojected_batches(
            channel=channel,
            limit=limit,
        ):
            items.extend(batch)
        return items

    def _delivery_outbox_by_run(self, run_id: str) -> DeliveryItem | None:
        return DeliveryOutboxStore(self.home).latest_for_run(run_id)

    def accepted_result_for_run(self, run_id: str) -> dict[str, Any]:
        item = self._delivery_outbox_by_run(run_id)
        if item is None:
            return {}
        return {
            "body": item.body,
            "body_provenance": item.body_provenance,
            "delivery_status": item.status,
            "channel": item.channel,
            "sent_at": item.sent_at,
            "outbox_id": item.id,
            "delivery_id": item.delivery_id,
            "delivery_attempts": item.attempts,
            "delivery_error": item.error,
            "delivery_error_reason": _delivery_error_reason(item.error),
            "delivery_updated_at": item.updated_at,
            "delivery_next_attempt_at": item.next_attempt_at,
            "delivery_expires_at": item.transport_context.get("expires_at", 0.0),
        }

    def record_delivery(
        self,
        *,
        run_id: str,
        channel: str,
        text_preview: str,
        text_length: int,
        media_count: int,
        trace_id: str = "",
        delivery_id: str = "",
        sent_at: float | None = None,
    ) -> GoalEvent | None:
        """Apply an authoritative connector receipt to loop, run, and goal state."""
        goal = self.get_by_run(run_id)
        if goal is None:
            return None
        recorded_at = time.time()
        evidence = {
            "state_transition": "delivered",
            "channel": channel,
            "sent_at": recorded_at if sent_at is None else float(sent_at),
            "recorded_at": recorded_at,
            "text_preview": text_preview,
            "text_length": max(0, int(text_length)),
            "media_count": max(0, int(media_count)),
            "goal_id": goal.id,
            "run_id": run_id,
            "delivery_id": delivery_id,
        }
        self._complete_delivery_loops(
            goal_id=goal.id,
            delivery_id=delivery_id,
            success=True,
            evidence=evidence,
        )
        from .runs import RunStore

        runs = RunStore(self.home)
        current_run = runs.get(run_id)
        updated_run = runs.update_run(
            run_id,
            phase=Phase.ENDED,
            governance=Governance.NONE,
            acceptance=Acceptance.ACCEPTED,
            resolution=Resolution.SUCCESS,
            result_summary=(current_run.result_summary if current_run else "") or text_preview,
            error="",
        )
        if updated_run is not None:
            goal = self.update_for_run(updated_run, evidence=evidence) or goal
        event = self.record_event(
            goal.id,
            "goal.delivery_succeeded",
            phase=Phase.ENDED,
            governance=goal.governance,
            acceptance=Acceptance.ACCEPTED,
            resolution=Resolution.SUCCESS,
            run_id=run_id,
            trace_id=trace_id or run_id,
            evidence=evidence,
        )
        if goal.parent_goal_id:
            parent = self.get(goal.parent_goal_id)
            if parent is not None:
                self.record_event(
                    parent.id,
                    "goal.occurrence_delivery_succeeded",
                    phase=parent.phase,
                    governance=parent.governance,
                    acceptance=parent.acceptance,
                    resolution=parent.resolution,
                    run_id=run_id,
                    trace_id=trace_id or run_id,
                    evidence=evidence,
                )
        return event

    def record_delivery_failure(
        self,
        *,
        run_id: str,
        channel: str,
        error: str,
        error_reason: str = "",
        trace_id: str = "",
        delivery_id: str = "",
    ) -> GoalEvent | None:
        """Apply an authoritative connector failure to loop, run, and goal state."""
        goal = self.get_by_run(run_id)
        if goal is None:
            return None
        evidence = {
            "state_transition": "delivery_failed",
            "channel": channel,
            "recorded_at": time.time(),
            "error": error,
            "error_reason": error_reason or _delivery_error_reason(error),
            "delivery_id": delivery_id,
            "goal_id": goal.id,
            "run_id": run_id,
        }
        self._complete_delivery_loops(
            goal_id=goal.id,
            delivery_id=delivery_id,
            success=False,
            evidence=evidence,
        )
        from .runs import RunStore

        runs = RunStore(self.home)
        updated_run = runs.update_run(
            run_id,
            phase=Phase.ENDED,
            governance=Governance.NONE,
            acceptance=Acceptance.REJECTED,
            resolution=Resolution.FAILED,
            result_summary="",
            error=error,
        )
        if updated_run is not None:
            goal = self.update_for_run(updated_run, evidence=evidence) or goal
        event = self.record_event(
            goal.id,
            "goal.delivery_failed",
            phase=Phase.ENDED,
            governance=goal.governance,
            acceptance=Acceptance.REJECTED,
            resolution=Resolution.FAILED,
            run_id=run_id,
            trace_id=trace_id or run_id,
            evidence=evidence,
        )
        if goal.parent_goal_id:
            parent = self.get(goal.parent_goal_id)
            if parent is not None:
                self.record_event(
                    parent.id,
                    "goal.occurrence_delivery_failed",
                    phase=parent.phase,
                    governance=parent.governance,
                    acceptance=parent.acceptance,
                    resolution=parent.resolution,
                    run_id=run_id,
                    trace_id=trace_id or run_id,
                    evidence=evidence,
                )
        return event

    def record_delivery_outcome_unknown(
        self,
        *,
        run_id: str,
        channel: str,
        error: str,
        trace_id: str = "",
        delivery_id: str = "",
    ) -> GoalEvent | None:
        """Record an interrupted transport attempt without guessing its outcome."""
        goal = self.get_by_run(run_id)
        if goal is None:
            return None
        evidence = {
            "state_transition": "delivery_outcome_unknown",
            "channel": channel,
            "recorded_at": time.time(),
            "error": error,
            "error_reason": "connector_delivery_outcome_unknown",
            "delivery_id": delivery_id,
            "goal_id": goal.id,
            "run_id": run_id,
        }
        from .runs import RunStore

        runs = RunStore(self.home)
        updated_run = runs.update_run(
            run_id,
            phase=Phase.PAUSED,
            governance=Governance.NONE,
            acceptance=Acceptance.UNVERIFIED,
            resolution=Resolution.BLOCKED,
            result_summary="",
            error=error,
        )
        if updated_run is not None:
            goal = self.update_for_run(updated_run, evidence=evidence) or goal
        event = self.record_event(
            goal.id,
            "goal.delivery_outcome_unknown",
            phase=goal.phase,
            governance=goal.governance,
            acceptance=goal.acceptance,
            resolution=goal.resolution,
            run_id=run_id,
            trace_id=trace_id or run_id,
            evidence=evidence,
        )
        if goal.parent_goal_id:
            parent = self.get(goal.parent_goal_id)
            if parent is not None:
                self.record_event(
                    parent.id,
                    "goal.occurrence_delivery_outcome_unknown",
                    phase=parent.phase,
                    governance=parent.governance,
                    acceptance=parent.acceptance,
                    resolution=parent.resolution,
                    run_id=run_id,
                    trace_id=trace_id or run_id,
                    evidence=evidence,
                )
        return event

    def _complete_delivery_loops(
        self,
        *,
        goal_id: str,
        delivery_id: str,
        success: bool,
        evidence: dict[str, Any],
    ) -> None:
        """Close the transport envelope and its originating delivery loop."""
        from .loop_contracts import LoopTerminalState
        from .loop_runs import LoopRunStore

        loop_runs = LoopRunStore(self.home)
        candidate_ids = {delivery_id} if delivery_id else set()
        for loop_run in loop_runs.list_by_goal_filtered(
            goal_id,
            terminal_states=(LoopTerminalState.PAUSED,),
            evidence_action="connector_outbound",
            limit=None,
        ):
            candidate_ids.add(loop_run.run_id)
        for loop_run_id in sorted(candidate_ids):
            delivery_loop = loop_runs.get_run(loop_run_id)
            if delivery_loop is None:
                continue
            loop_runs.complete_external_delivery(
                loop_run_id,
                success=success,
                evidence=evidence,
            )
            if delivery_loop.goal_id != goal_id:
                self._settle_delivery_envelope_goal(
                    goal_id=delivery_loop.goal_id,
                    success=success,
                    evidence=evidence,
                )

    def _settle_delivery_envelope_goal(
        self,
        *,
        goal_id: str,
        success: bool,
        evidence: dict[str, Any],
    ) -> None:
        """Settle the user-turn Goal/Run that transported another goal's delivery."""
        goal = self.get(goal_id)
        if goal is None or not goal.run_id:
            return
        from .runs import RunStore

        runs = RunStore(self.home)
        current = runs.get(goal.run_id)
        if current is None:
            return
        envelope_evidence = {
            **evidence,
            "transport_envelope_goal_id": goal_id,
            "origin_goal_id": str(evidence.get("goal_id") or ""),
        }
        updated = runs.update_run(
            current.id,
            phase=Phase.ENDED,
            governance=Governance.NONE,
            acceptance=Acceptance.ACCEPTED if success else Acceptance.REJECTED,
            resolution=Resolution.SUCCESS if success else Resolution.FAILED,
            result_summary=current.result_summary if success else "",
            error="" if success else str(evidence.get("error") or "delivery failed"),
        )
        if updated is not None:
            self.update_for_run(updated, evidence=envelope_evidence)

    def latest_delivery(self, goal_id: str) -> dict[str, Any]:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT evidence_json FROM goal_events
                WHERE goal_id = ? AND event_type = 'goal.delivery_succeeded'
                ORDER BY created_at DESC LIMIT 1
                """,
                (goal_id,),
            ).fetchone()
        return _json_object(row[0]) if row else {}

    def list_recent_deliveries(
        self,
        *,
        source: str = "",
        peer_id: str = "",
        sender_id: str = "",
        limit: int = 20,
    ) -> builtins.list[dict[str, Any]]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT e.evidence_json
                FROM goal_events e
                JOIN goals g ON g.id = e.goal_id
                WHERE e.event_type = 'goal.delivery_succeeded'
                  AND (? = '' OR g.source = ?)
                  AND (? = '' OR g.peer_id = ?)
                  AND (? = '' OR g.sender_id = ?)
                  AND e.created_at = (
                      SELECT MAX(latest.created_at)
                      FROM goal_events latest
                      WHERE latest.goal_id = e.goal_id
                        AND latest.event_type = 'goal.delivery_succeeded'
                  )
                ORDER BY e.created_at DESC LIMIT ?
                """,
                (source, source, peer_id, peer_id, sender_id, sender_id, max(1, limit)),
            ).fetchall()
        return [_json_object(row[0]) for row in rows]

    def find_active_cron_goal(
        self,
        *,
        objective: str,
        cron_schedule: str,
        source: str,
        peer_id: str,
        sender_id: str,
    ) -> Goal | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"""
                SELECT {GOAL_SELECT_LIST}
                FROM goals
                WHERE objective = ? AND cron_schedule = ? AND source = ?
                  AND peer_id = ? AND sender_id = ? AND phase != ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (objective, cron_schedule, source, peer_id, sender_id, Phase.ENDED),
            ).fetchone()
        return Goal(*row) if row else None

    def find_active_cron_goal_by_schedule(
        self,
        *,
        cron_schedule: str,
        source: str,
        peer_id: str,
        sender_id: str,
        exclude_goal_id: str = "",
    ) -> Goal | None:
        clauses = [
            "cron_schedule = ?",
            "source = ?",
            "peer_id = ?",
            "sender_id = ?",
            "phase != ?",
        ]
        params: list[Any] = [cron_schedule, source, peer_id, sender_id, Phase.ENDED]
        if exclude_goal_id:
            clauses.append("id != ?")
            params.append(exclude_goal_id)
        with connect(self.db_path) as conn:
            row = conn.execute(
                f"""
                SELECT {GOAL_SELECT_LIST}
                FROM goals
                WHERE {" AND ".join(clauses)}
                ORDER BY created_at DESC LIMIT 1
                """,
                params,
            ).fetchone()
        return Goal(*row) if row else None

    def list_cron_goals(self) -> builtins.list[Goal]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT {GOAL_SELECT_LIST}
                FROM goals
                WHERE cron_schedule != ''
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [Goal(*row) for row in rows]

    def due_cron_goals(self, now: float) -> builtins.list[Goal]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT {GOAL_SELECT_LIST}
                FROM goals
                WHERE cron_schedule != '' AND next_run_at <= ? AND phase != ?
                ORDER BY next_run_at ASC
                """,
                (now, Phase.ENDED),
            ).fetchall()
        return [Goal(*row) for row in rows]

    def update_cron_run(self, goal_id: str, next_run_at: float) -> Goal | None:
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE goals SET next_run_at = ?, updated_at = ? WHERE id = ?",
                (next_run_at, time.time(), goal_id),
            )
        return self.get(goal_id)

    def update_scheduled_template(
        self,
        goal_id: str,
        *,
        objective: str,
        cron_schedule: str,
        next_run_at: float,
        evidence: dict[str, Any] | None = None,
        event_type: str = "goal.updated",
    ) -> Goal | None:
        goal = self.get(goal_id)
        if goal is None:
            return None
        merged_evidence = _merge_evidence(goal.evidence_json, evidence)
        now = time.time()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE goals
                SET objective = ?, cron_schedule = ?, next_run_at = ?,
                    evidence_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    objective,
                    cron_schedule,
                    next_run_at,
                    json.dumps(merged_evidence, ensure_ascii=False, sort_keys=True),
                    now,
                    goal_id,
                ),
            )
        updated = self.get(goal_id)
        self.record_event(
            goal_id,
            event_type,
            phase=updated.phase if updated else goal.phase,
            governance=updated.governance if updated else goal.governance,
            acceptance=updated.acceptance if updated else goal.acceptance,
            resolution=updated.resolution if updated else goal.resolution,
            run_id=updated.run_id if updated else goal.run_id,
            trace_id=updated.trace_id if updated else goal.trace_id,
            evidence=evidence or {},
        )
        return updated

    def record_cron_failure(
        self,
        goal_id: str,
        *,
        scheduled_for: float,
        next_run_at: float,
        error_type: str,
        error: str,
        trace_id: str,
    ) -> Goal | None:
        """Advance a failed recurring template and preserve one durable failure fact."""
        goal = self.get(goal_id)
        if goal is None:
            return None
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE goals SET next_run_at = ?, updated_at = ? WHERE id = ?",
                (next_run_at, time.time(), goal_id),
            )
        self.record_event(
            goal_id,
            "goal.schedule_occurrence_failed",
            phase=goal.phase,
            governance=goal.governance,
            acceptance=goal.acceptance,
            resolution=goal.resolution,
            run_id=goal.run_id,
            trace_id=trace_id,
            evidence={
                "scheduled_for": scheduled_for,
                "next_run_at": next_run_at,
                "error_type": error_type,
                "error": error,
            },
        )
        return self.get(goal_id)

    def block_invalid_cron_schedule(
        self,
        goal_id: str,
        *,
        scheduled_for: float,
        error_type: str,
        error: str,
        trace_id: str,
    ) -> Goal | None:
        """Fail closed when persisted recurring state violates the cron contract."""
        goal = self.get(goal_id)
        if goal is None:
            return None
        now = time.time()
        evidence = {
            "scheduled_for": scheduled_for,
            "cron_schedule": goal.cron_schedule,
            "error_type": error_type,
            "error": error,
            "state_transition": "schedule_blocked",
        }
        merged_evidence = _merge_evidence(goal.evidence_json, evidence)
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE goals
                SET phase = ?, resolution = ?, task_status = ?, blocked_reason = ?,
                    next_run_at = 0.0, evidence_json = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    Phase.ENDED,
                    Resolution.BLOCKED,
                    "blocked",
                    "invalid_cron_schedule",
                    json.dumps(merged_evidence, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                    goal_id,
                ),
            )
        self.record_event(
            goal_id,
            "goal.schedule_invalid",
            phase=Phase.ENDED,
            governance=goal.governance,
            acceptance=goal.acceptance,
            resolution=Resolution.BLOCKED,
            run_id=goal.run_id,
            trace_id=trace_id,
            evidence=evidence,
        )
        return self.get(goal_id)


def _goal_state_for_run(
    run: Run, *, evidence: dict[str, Any] | None = None
) -> tuple[str, str, str, str]:
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


def _json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _delivery_error_reason(error: str) -> str:
    text = str(error or "")
    prefix = text.partition(":")[0].strip()
    if prefix.startswith("connector_"):
        return prefix
    return "connector_delivery_failed" if error else ""


def _delivery_outbox_from_row(row: tuple[Any, ...]) -> GoalDeliveryOutboxItem:
    return GoalDeliveryOutboxItem(
        id=str(row[0]),
        goal_id=str(row[1]),
        run_id=str(row[2]),
        channel=str(row[3]),
        source=str(row[4]),
        peer_id=str(row[5]),
        sender_id=str(row[6]),
        trace_id=str(row[7]),
        body=str(row[8]),
        body_provenance=str(row[9]),
        status=str(row[10]),
        attempts=int(row[11]),
        error=str(row[12]),
        delivery_id=str(row[13]),
        created_at=float(row[14]),
        updated_at=float(row[15]),
        sent_at=float(row[16]),
    )


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
        # Persistent task tree. parent_goal_id enables a
        # parent-child hierarchy so the engine can track sub-task
        # decomposition across long-horizon tasks. task_status is the
        # explicit lifecycle state (pending/in_progress/done/blocked)
        # that the model updates as it progresses.
        Column("parent_goal_id", "TEXT", nullable=False),
        Column("task_status", "TEXT", nullable=False),
        Column("cron_schedule", "TEXT", nullable=False),
        Column("next_run_at", "REAL", nullable=False),
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

GOAL_DELIVERY_OUTBOX_TABLE = Table(
    "goal_delivery_outbox",
    [
        Column("id", "TEXT", primary_key=True),
        Column("goal_id", "TEXT", nullable=False),
        Column("run_id", "TEXT", nullable=False, unique=True),
        Column("channel", "TEXT", nullable=False),
        Column("source", "TEXT", nullable=False),
        Column("peer_id", "TEXT", nullable=False),
        Column("sender_id", "TEXT", nullable=False),
        Column("trace_id", "TEXT", nullable=False),
        Column("body", "TEXT", nullable=False),
        Column("body_provenance", "TEXT", nullable=False),
        Column("status", "TEXT", nullable=False),
        Column("attempts", "INTEGER", nullable=False),
        Column("error", "TEXT", nullable=False),
        Column("delivery_id", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
        Column("updated_at", "REAL", nullable=False),
        Column("sent_at", "REAL", nullable=False),
    ],
)
