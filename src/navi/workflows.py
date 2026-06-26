from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect, ensure_schema_version
from .paths import db_paths
from .schema import Column, Table, assert_schema_exact


WORKFLOW_STORE_SCHEMA_VERSION = 1

WORKFLOW_STATUS_AWAITING_APPROVAL = "awaiting_approval"
WORKFLOW_STATUS_APPROVED = "approved"
WORKFLOW_STATUS_RUNNING = "running"
WORKFLOW_STATUS_INTERRUPTED = "interrupted"
WORKFLOW_STATUS_COMPLETED = "completed"
WORKFLOW_STATUS_VERIFIED_COMPLETE = "verified_complete"
WORKFLOW_STATUS_BLOCKED = "blocked"
WORKFLOW_STATUS_REJECTED = "rejected"

STEP_STATUS_PENDING = "pending"
STEP_STATUS_RUNNING = "running"
STEP_STATUS_COMPLETED = "completed"
STEP_STATUS_FAILED = "failed"
STEP_STATUS_BLOCKED = "blocked"

WORKFLOW_STATUSES = {
    WORKFLOW_STATUS_AWAITING_APPROVAL,
    WORKFLOW_STATUS_APPROVED,
    WORKFLOW_STATUS_RUNNING,
    WORKFLOW_STATUS_INTERRUPTED,
    WORKFLOW_STATUS_COMPLETED,
    WORKFLOW_STATUS_VERIFIED_COMPLETE,
    WORKFLOW_STATUS_BLOCKED,
    WORKFLOW_STATUS_REJECTED,
}
STEP_STATUSES = {
    STEP_STATUS_PENDING,
    STEP_STATUS_RUNNING,
    STEP_STATUS_COMPLETED,
    STEP_STATUS_FAILED,
    STEP_STATUS_BLOCKED,
}


@dataclass(frozen=True)
class Workflow:
    id: str
    objective: str
    status: str
    source: str
    peer_id: str
    sender_id: str
    workspace: str
    permission_ceiling: str
    max_concurrency: int
    total_subagent_limit: int
    risk_class: str
    estimated_cost: str
    stop_condition: str
    verification_strategy: str
    plan_json: str
    evidence_json: str
    blocked_reason: str
    created_at: float
    updated_at: float
    completed_at: float


@dataclass(frozen=True)
class WorkflowStep:
    id: str
    workflow_id: str
    seq: int
    role: str
    objective: str
    status: str
    depends_on_json: str
    allowed_tools_json: str
    tool_calls_json: str
    evidence_json: str
    error: str
    started_at: float
    updated_at: float
    completed_at: float


@dataclass(frozen=True)
class WorkflowEvent:
    id: str
    workflow_id: str
    event_type: str
    status: str
    step_id: str
    evidence_json: str
    created_at: float


@dataclass(frozen=True)
class WorkflowTransitionDecision:
    status: str
    event_type: str
    blocked_reason: str = ""
    evidence: dict[str, Any] | None = None


@dataclass(frozen=True)
class WorkflowVerificationDecision:
    passed: bool
    status: str
    event_type: str
    blocked_reason: str
    output: dict[str, Any]


WORKFLOW_RUNNABLE_STATUSES = frozenset(
    {
        WORKFLOW_STATUS_APPROVED,
        WORKFLOW_STATUS_RUNNING,
        WORKFLOW_STATUS_INTERRUPTED,
    }
)


def workflow_can_run(status: str) -> bool:
    return status in WORKFLOW_RUNNABLE_STATUSES


def workflow_batch_transition(
    *,
    completed: int,
    failed: int,
    pending_count: int,
) -> WorkflowTransitionDecision:
    if failed:
        return WorkflowTransitionDecision(
            status=WORKFLOW_STATUS_BLOCKED,
            event_type="workflow.blocked",
            blocked_reason="one or more workflow steps failed",
            evidence={"completed_in_batch": completed, "failed_in_batch": failed},
        )
    if pending_count == 0:
        return WorkflowTransitionDecision(
            status=WORKFLOW_STATUS_COMPLETED,
            event_type="workflow.completed",
            evidence={"completed_in_batch": completed},
        )
    return WorkflowTransitionDecision(
        status=WORKFLOW_STATUS_INTERRUPTED,
        event_type="workflow.interrupted",
        evidence={"completed_in_batch": completed, "pending_count": pending_count},
    )


def workflow_idle_transition(counts: dict[str, int]) -> WorkflowTransitionDecision | None:
    if counts.get("pending_count") == 0 and counts.get("failed_count") == 0:
        return WorkflowTransitionDecision(
            status=WORKFLOW_STATUS_COMPLETED,
            event_type="workflow.completed",
            evidence=counts,
        )
    return None


def workflow_verification_decision(
    *,
    workflow: Workflow,
    steps: list[WorkflowStep],
) -> WorkflowVerificationDecision:
    workflow_plan = _json_dict(workflow.plan_json)
    goal_type = str(workflow_plan.get("goal_type") or "").strip().lower()
    failed_steps = [step for step in steps if step.status != STEP_STATUS_COMPLETED]
    empty_evidence = [step.id for step in steps if not _json_dict(step.evidence_json)]
    capability_steps = [step.id for step in steps if _step_has_execution_evidence(step)]
    missing_execution_evidence = not capability_steps and goal_type != "planning"
    passed = (
        workflow.status in (WORKFLOW_STATUS_COMPLETED, WORKFLOW_STATUS_VERIFIED_COMPLETE)
        and not failed_steps
        and not empty_evidence
        and not missing_execution_evidence
    )
    blocked_reason = ""
    if not passed:
        blocked_reason = "workflow verifier requires completed workflow, completed steps, and non-empty step evidence"
        if missing_execution_evidence:
            blocked_reason = "workflow verifier requires capability execution evidence unless plan.goal_type is planning"
    output = {
        "workflow_id": workflow.id,
        "passed": passed,
        "failed_steps": [step.id for step in failed_steps],
        "empty_evidence_steps": empty_evidence,
        "capability_step_count": len(capability_steps),
        "goal_type": goal_type,
    }
    return WorkflowVerificationDecision(
        passed=passed,
        status=WORKFLOW_STATUS_VERIFIED_COMPLETE if passed else WORKFLOW_STATUS_BLOCKED,
        event_type="workflow.verified" if passed else "workflow.verifier_blocked",
        blocked_reason=blocked_reason,
        output=output,
    )


def _json_dict(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _step_has_execution_evidence(step: WorkflowStep) -> bool:
    evidence = _json_dict(step.evidence_json)
    items = evidence.get("evidence")
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("tool") or "").strip():
            return True
        if item.get("kind") == "model_step" and str(item.get("trace_id") or "").strip():
            return True
    return False


class WorkflowStore:
    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = db_paths(home).workflows
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            ensure_schema_version(conn, "workflows", WORKFLOW_STORE_SCHEMA_VERSION)
            conn.execute(WORKFLOWS_TABLE.ddl)
            assert_schema_exact(conn, WORKFLOWS_TABLE)
            conn.execute(WORKFLOW_STEPS_TABLE.ddl)
            assert_schema_exact(conn, WORKFLOW_STEPS_TABLE)
            conn.execute(WORKFLOW_EVENTS_TABLE.ddl)
            assert_schema_exact(conn, WORKFLOW_EVENTS_TABLE)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflows_status ON workflows(status, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflow_steps_workflow ON workflow_steps(workflow_id, seq)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflow_events_workflow ON workflow_events(workflow_id, created_at)"
            )

    def create(
        self,
        *,
        objective: str,
        workspace: str,
        source: str = "",
        peer_id: str = "",
        sender_id: str = "",
        permission_ceiling: str = "read",
        max_concurrency: int = 4,
        total_subagent_limit: int = 32,
        risk_class: str = "",
        estimated_cost: str = "",
        stop_condition: str = "",
        verification_strategy: str = "",
        plan: dict[str, Any] | None = None,
        steps: list[dict[str, Any]] | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> Workflow:
        now = time.time()
        workflow_id = uuid.uuid4().hex
        permission_ceiling = _permission_ceiling(permission_ceiling)
        normalized_steps = _normalize_steps(
            steps or [], workflow_id=workflow_id, total_limit=total_subagent_limit
        )
        if not normalized_steps:
            normalized_steps = _normalize_steps(
                [
                    {
                        "role": "planner",
                        "objective": objective,
                        "allowed_tools": [],
                        "tool_calls": [],
                    }
                ],
                workflow_id=workflow_id,
                total_limit=total_subagent_limit,
            )
        plan_data = {
            **(plan or {}),
            "steps": [
                {
                    "id": step.id,
                    "seq": step.seq,
                    "role": step.role,
                    "objective": step.objective,
                    "depends_on": _loads(step.depends_on_json, []),
                    "allowed_tools": _loads(step.allowed_tools_json, []),
                    "tool_calls": _loads(step.tool_calls_json, []),
                }
                for step in normalized_steps
            ],
        }
        workflow = Workflow(
            id=workflow_id,
            objective=objective.strip(),
            status=WORKFLOW_STATUS_AWAITING_APPROVAL,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            workspace=_require_workspace(workspace),
            permission_ceiling=permission_ceiling,
            max_concurrency=max(1, min(int(max_concurrency or 4), 16)),
            total_subagent_limit=max(1, min(int(total_subagent_limit or 32), 1000)),
            risk_class=risk_class or _risk_class(permission_ceiling),
            estimated_cost=estimated_cost or "unknown",
            stop_condition=stop_condition or "all declared steps complete and verifier passes",
            verification_strategy=verification_strategy
            or "independent verifier checks every step has evidence and no failed capability result",
            plan_json=json.dumps(plan_data, ensure_ascii=False, sort_keys=True),
            evidence_json=json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
            blocked_reason="",
            created_at=now,
            updated_at=now,
            completed_at=0.0,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO workflows(
                    id, objective, status, source, peer_id, sender_id, workspace,
                    permission_ceiling, max_concurrency, total_subagent_limit,
                    risk_class, estimated_cost, stop_condition, verification_strategy,
                    plan_json, evidence_json, blocked_reason, created_at, updated_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow.id,
                    workflow.objective,
                    workflow.status,
                    workflow.source,
                    workflow.peer_id,
                    workflow.sender_id,
                    workflow.workspace,
                    workflow.permission_ceiling,
                    workflow.max_concurrency,
                    workflow.total_subagent_limit,
                    workflow.risk_class,
                    workflow.estimated_cost,
                    workflow.stop_condition,
                    workflow.verification_strategy,
                    workflow.plan_json,
                    workflow.evidence_json,
                    workflow.blocked_reason,
                    workflow.created_at,
                    workflow.updated_at,
                    workflow.completed_at,
                ),
            )
            for step in normalized_steps:
                conn.execute(
                    """
                    INSERT INTO workflow_steps(
                        id, workflow_id, seq, role, objective, status, depends_on_json,
                        allowed_tools_json, tool_calls_json, evidence_json, error,
                        started_at, updated_at, completed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        step.id,
                        step.workflow_id,
                        step.seq,
                        step.role,
                        step.objective,
                        step.status,
                        step.depends_on_json,
                        step.allowed_tools_json,
                        step.tool_calls_json,
                        step.evidence_json,
                        step.error,
                        step.started_at,
                        step.updated_at,
                        step.completed_at,
                    ),
                )
        self.record_event(
            workflow.id, "workflow.proposed", status=workflow.status, evidence=evidence or {}
        )
        return workflow

    def get(self, workflow_id: str) -> Workflow | None:
        with connect(self.db_path) as conn:
            row = conn.execute(_SELECT_WORKFLOW + " WHERE id = ?", (workflow_id,)).fetchone()
        return Workflow(*row) if row else None

    def list(self, *, status: str = "", limit: int = 50) -> list[Workflow]:
        if status:
            query = _SELECT_WORKFLOW + " WHERE status = ? ORDER BY updated_at DESC LIMIT ?"
            params: tuple[Any, ...] = (status, limit)
        else:
            query = _SELECT_WORKFLOW + " ORDER BY updated_at DESC LIMIT ?"
            params = (limit,)
        with connect(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [Workflow(*row) for row in rows]

    def list_steps(self, workflow_id: str) -> list[WorkflowStep]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                _SELECT_STEP + " WHERE workflow_id = ? ORDER BY seq ASC", (workflow_id,)
            ).fetchall()
        return [WorkflowStep(*row) for row in rows]

    def list_events(self, workflow_id: str, *, limit: int = 200) -> list[WorkflowEvent]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                _SELECT_EVENT + " WHERE workflow_id = ? ORDER BY created_at ASC LIMIT ?",
                (workflow_id, limit),
            ).fetchall()
        return [WorkflowEvent(*row) for row in rows]

    def update_status(
        self,
        workflow_id: str,
        *,
        status: str,
        blocked_reason: str = "",
        evidence: dict[str, Any] | None = None,
        event_type: str = "workflow.status",
    ) -> Workflow | None:
        if status not in WORKFLOW_STATUSES:
            raise ValueError(f"unsupported workflow status: {status}")
        workflow = self.get(workflow_id)
        if workflow is None:
            return None
        merged = _merge_evidence(workflow.evidence_json, evidence)
        now = time.time()
        completed_at = (
            now
            if status
            in {
                WORKFLOW_STATUS_VERIFIED_COMPLETE,
                WORKFLOW_STATUS_BLOCKED,
                WORKFLOW_STATUS_REJECTED,
            }
            else workflow.completed_at
        )
        if status == WORKFLOW_STATUS_COMPLETED and not completed_at:
            completed_at = now
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE workflows
                SET status = ?, blocked_reason = ?, evidence_json = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    blocked_reason,
                    json.dumps(merged, ensure_ascii=False, sort_keys=True),
                    now,
                    completed_at,
                    workflow_id,
                ),
            )
        self.record_event(workflow_id, event_type, status=status, evidence=evidence or {})
        return self.get(workflow_id)

    def update_step(
        self,
        step_id: str,
        *,
        status: str,
        evidence: dict[str, Any] | None = None,
        error: str = "",
    ) -> WorkflowStep | None:
        if status not in STEP_STATUSES:
            raise ValueError(f"unsupported workflow step status: {status}")
        step = self.get_step(step_id)
        if step is None:
            return None
        merged = _merge_evidence(step.evidence_json, evidence)
        now = time.time()
        started_at = (
            now if step.started_at == 0.0 and status == STEP_STATUS_RUNNING else step.started_at
        )
        completed_at = (
            now
            if status in {STEP_STATUS_COMPLETED, STEP_STATUS_FAILED, STEP_STATUS_BLOCKED}
            else step.completed_at
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE workflow_steps
                SET status = ?, evidence_json = ?, error = ?, started_at = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(merged, ensure_ascii=False, sort_keys=True),
                    error,
                    started_at,
                    now,
                    completed_at,
                    step_id,
                ),
            )
        self.record_event(
            step.workflow_id,
            f"workflow.step.{status}",
            status=status,
            step_id=step_id,
            evidence=evidence or {},
        )
        return self.get_step(step_id)

    def get_step(self, step_id: str) -> WorkflowStep | None:
        with connect(self.db_path) as conn:
            row = conn.execute(_SELECT_STEP + " WHERE id = ?", (step_id,)).fetchone()
        return WorkflowStep(*row) if row else None

    def record_event(
        self,
        workflow_id: str,
        event_type: str,
        *,
        status: str,
        step_id: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> WorkflowEvent:
        event = WorkflowEvent(
            id=uuid.uuid4().hex,
            workflow_id=workflow_id,
            event_type=event_type,
            status=status,
            step_id=step_id,
            evidence_json=json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
            created_at=time.time(),
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO workflow_events(id, workflow_id, event_type, status, step_id, evidence_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.workflow_id,
                    event.event_type,
                    event.status,
                    event.step_id,
                    event.evidence_json,
                    event.created_at,
                ),
            )
        return event


def workflow_facts(store: WorkflowStore, workflow: Workflow) -> dict[str, Any]:
    steps = store.list_steps(workflow.id)
    events = store.list_events(workflow.id)
    return {
        "workflow": _workflow_dict(workflow),
        "steps": [_step_dict(step) for step in steps],
        "events": [_event_dict(event) for event in events],
        "step_count": len(steps),
        "pending_count": len([step for step in steps if step.status == STEP_STATUS_PENDING]),
        "completed_count": len([step for step in steps if step.status == STEP_STATUS_COMPLETED]),
        "failed_count": len(
            [step for step in steps if step.status in {STEP_STATUS_FAILED, STEP_STATUS_BLOCKED}]
        ),
    }


def _workflow_dict(workflow: Workflow) -> dict[str, Any]:
    data = workflow.__dict__.copy()
    data["plan"] = _loads(workflow.plan_json, {})
    data["evidence"] = _loads(workflow.evidence_json, {})
    return data


def _step_dict(step: WorkflowStep) -> dict[str, Any]:
    data = step.__dict__.copy()
    data["depends_on"] = _loads(step.depends_on_json, [])
    data["allowed_tools"] = _loads(step.allowed_tools_json, [])
    data["tool_calls"] = _loads(step.tool_calls_json, [])
    data["evidence"] = _loads(step.evidence_json, {})
    return data


def _event_dict(event: WorkflowEvent) -> dict[str, Any]:
    data = event.__dict__.copy()
    data["evidence"] = _loads(event.evidence_json, {})
    return data


def _normalize_steps(
    raw_steps: list[dict[str, Any]], *, workflow_id: str, total_limit: int
) -> list[WorkflowStep]:
    steps: list[WorkflowStep] = []
    now = time.time()
    for index, raw in enumerate(raw_steps[: max(1, min(int(total_limit or 32), 1000))], start=1):
        if not isinstance(raw, dict):
            continue
        objective = str(raw.get("objective") or raw.get("prompt") or "").strip()
        if not objective:
            continue
        role = str(raw.get("role") or "worker").strip() or "worker"
        depends_on = raw.get("depends_on") if isinstance(raw.get("depends_on"), list) else []
        allowed_tools = (
            raw.get("allowed_tools") if isinstance(raw.get("allowed_tools"), list) else []
        )
        tool_calls = raw.get("tool_calls") if isinstance(raw.get("tool_calls"), list) else []
        steps.append(
            WorkflowStep(
                id=str(raw.get("id") or uuid.uuid4().hex),
                workflow_id=workflow_id,
                seq=int(raw.get("seq") or index),
                role=role[:64],
                objective=objective,
                status=STEP_STATUS_PENDING,
                depends_on_json=json.dumps(
                    [str(item) for item in depends_on], ensure_ascii=False, sort_keys=True
                ),
                allowed_tools_json=json.dumps(
                    [str(item) for item in allowed_tools], ensure_ascii=False, sort_keys=True
                ),
                tool_calls_json=json.dumps(
                    [item for item in tool_calls if isinstance(item, dict)],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                evidence_json="{}",
                error="",
                started_at=0.0,
                updated_at=now,
                completed_at=0.0,
            )
        )
    return sorted(steps, key=lambda step: step.seq)


def _permission_ceiling(value: str) -> str:
    raw = str(value or "read").strip()
    return raw if raw in {"read", "prepare", "write"} else "read"


def _risk_class(permission_ceiling: str) -> str:
    if permission_ceiling == "write":
        return "high"
    if permission_ceiling == "prepare":
        return "medium"
    return "low"


def _require_workspace(workspace: str) -> str:
    value = str(workspace or "").strip()
    if not value:
        raise ValueError("workspace is required")
    return value


def _loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _json_list(value: str) -> list[Any]:
    parsed = _loads(value, [])
    return parsed if isinstance(parsed, list) else []


def _merge_evidence(existing_json: str, evidence: dict[str, Any] | None) -> dict[str, Any]:
    existing = _loads(existing_json, {})
    if not isinstance(existing, dict):
        existing = {}
    if evidence:
        existing.update(evidence)
    return existing


WORKFLOWS_TABLE = Table(
    "workflows",
    [
        Column("id", "TEXT", primary_key=True),
        Column("objective", "TEXT", nullable=False),
        Column("status", "TEXT", nullable=False),
        Column("source", "TEXT", nullable=False),
        Column("peer_id", "TEXT", nullable=False),
        Column("sender_id", "TEXT", nullable=False),
        Column("workspace", "TEXT", nullable=False),
        Column("permission_ceiling", "TEXT", nullable=False),
        Column("max_concurrency", "INTEGER", nullable=False),
        Column("total_subagent_limit", "INTEGER", nullable=False),
        Column("risk_class", "TEXT", nullable=False),
        Column("estimated_cost", "TEXT", nullable=False),
        Column("stop_condition", "TEXT", nullable=False),
        Column("verification_strategy", "TEXT", nullable=False),
        Column("plan_json", "TEXT", nullable=False),
        Column("evidence_json", "TEXT", nullable=False),
        Column("blocked_reason", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
        Column("updated_at", "REAL", nullable=False),
        Column("completed_at", "REAL", nullable=False),
    ],
)
WORKFLOW_STEPS_TABLE = Table(
    "workflow_steps",
    [
        Column("id", "TEXT", primary_key=True),
        Column("workflow_id", "TEXT", nullable=False),
        Column("seq", "INTEGER", nullable=False),
        Column("role", "TEXT", nullable=False),
        Column("objective", "TEXT", nullable=False),
        Column("status", "TEXT", nullable=False),
        Column("depends_on_json", "TEXT", nullable=False),
        Column("allowed_tools_json", "TEXT", nullable=False),
        Column("tool_calls_json", "TEXT", nullable=False),
        Column("evidence_json", "TEXT", nullable=False),
        Column("error", "TEXT", nullable=False),
        Column("started_at", "REAL", nullable=False),
        Column("updated_at", "REAL", nullable=False),
        Column("completed_at", "REAL", nullable=False),
    ],
)
WORKFLOW_EVENTS_TABLE = Table(
    "workflow_events",
    [
        Column("id", "TEXT", primary_key=True),
        Column("workflow_id", "TEXT", nullable=False),
        Column("event_type", "TEXT", nullable=False),
        Column("status", "TEXT", nullable=False),
        Column("step_id", "TEXT", nullable=False),
        Column("evidence_json", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
    ],
)
_SELECT_WORKFLOW = """
    SELECT id, objective, status, source, peer_id, sender_id, workspace,
           permission_ceiling, max_concurrency, total_subagent_limit,
           risk_class, estimated_cost, stop_condition, verification_strategy,
           plan_json, evidence_json, blocked_reason, created_at, updated_at, completed_at
    FROM workflows
"""
_SELECT_STEP = """
    SELECT id, workflow_id, seq, role, objective, status, depends_on_json,
           allowed_tools_json, tool_calls_json, evidence_json, error,
           started_at, updated_at, completed_at
    FROM workflow_steps
"""
_SELECT_EVENT = """
    SELECT id, workflow_id, event_type, status, step_id, evidence_json, created_at
    FROM workflow_events
"""
