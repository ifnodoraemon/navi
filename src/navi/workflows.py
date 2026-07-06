from __future__ import annotations
from .lifecycle import Phase, Governance, Acceptance, Resolution

import json
import typing
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import connect, check_schema_version, write_schema_version
from .loop import LoopCheckName, LoopSeverity
from .paths import db_paths
from .schema import Column, Table, assert_schema_exact


WORKFLOW_STORE_SCHEMA_VERSION = 2



@dataclass(frozen=True)
class Workflow:
    id: str
    objective: str
    phase: str
    governance: str
    acceptance: str
    resolution: str
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
    phase: str
    governance: str
    acceptance: str
    resolution: str
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
    phase: str
    governance: str
    acceptance: str
    resolution: str
    step_id: str
    evidence_json: str
    created_at: float


@dataclass(frozen=True)
class WorkflowTransitionDecision:
    phase: str
    governance: str
    acceptance: str
    resolution: str
    event_type: str
    blocked_reason: str = ""
    evidence: dict[str, Any] | None = None


@dataclass(frozen=True)
class WorkflowVerificationDecision:
    passed: bool
    phase: str
    governance: str
    acceptance: str
    resolution: str
    event_type: str
    blocked_reason: str
    output: dict[str, Any]
    check_results: tuple["WorkflowCheckResult", ...] = ()


@dataclass(frozen=True)
class WorkflowCheckResult:
    name: str
    passed: bool
    severity: str = "info"
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "severity": self.severity,
            "reason": self.reason,
            "evidence": self.evidence,
        }


def workflow_can_run(phase: str, governance: str) -> bool:
    return governance == Governance.APPROVED and phase in (Phase.RUNNING, Phase.PAUSED)


def workflow_batch_transition(
    *,
    completed: int,
    failed: int,
    pending_count: int,
) -> WorkflowTransitionDecision:
    if failed:
        return WorkflowTransitionDecision(
            phase=Phase.ENDED, governance=Governance.NONE, acceptance=Acceptance.NONE, resolution=Resolution.BLOCKED,
            event_type="workflow.blocked",
            blocked_reason="workflow_step_failed",
            evidence={"completed_in_batch": completed, "failed_in_batch": failed},
        )
    if pending_count == 0:
        return WorkflowTransitionDecision(
            phase=Phase.ENDED, governance=Governance.NONE, acceptance=Acceptance.NONE, resolution=Resolution.SUCCESS,
            event_type="workflow.completed",
            evidence={"completed_in_batch": completed},
        )
    return WorkflowTransitionDecision(
        phase=Phase.PAUSED, governance=Governance.NONE, acceptance=Acceptance.NONE, resolution=Resolution.BLOCKED,
        event_type="workflow.interrupted",
        evidence={"completed_in_batch": completed, "pending_count": pending_count},
    )


def workflow_idle_transition(counts: dict[str, int]) -> WorkflowTransitionDecision | None:
    if counts.get("pending_count") == 0 and counts.get("failed_count") == 0:
        return WorkflowTransitionDecision(
            phase=Phase.ENDED, governance=Governance.NONE, acceptance=Acceptance.NONE, resolution=Resolution.SUCCESS,
            event_type="workflow.completed",
            evidence=counts,
        )
    return None


def workflow_verification_decision(
    *,
    workflow: Workflow,
    steps: list["WorkflowStep"],
) -> WorkflowVerificationDecision:
    workflow_plan = _json_dict(workflow.plan_json)
    goal_type = str(workflow_plan.get("goal_type") or "").strip().lower()
    failed_steps = [
        step for step in steps
        if step.phase != Phase.ENDED or step.resolution != Resolution.SUCCESS
    ]
    empty_evidence = [step.id for step in steps if not _json_dict(step.evidence_json)]
    capability_steps = [step.id for step in steps if _step_has_execution_evidence(step)]
    missing_execution_evidence = not capability_steps and goal_type != "planning"
    workflow_succeeded = workflow.phase == Phase.ENDED and workflow.resolution == Resolution.SUCCESS
    check_results = (
        WorkflowCheckResult(
            name=LoopCheckName.WORKFLOW_RESOLUTION_SUCCESS,
            passed=workflow_succeeded,
            severity=LoopSeverity.ERROR if not workflow_succeeded else LoopSeverity.INFO,
            reason=(
                "workflow_resolution_success"
                if workflow_succeeded
                else "workflow_resolution_not_success"
            ),
            evidence={"phase": workflow.phase, "resolution": workflow.resolution},
        ),
        WorkflowCheckResult(
            name=LoopCheckName.WORKFLOW_STEPS_COMPLETED,
            passed=not failed_steps,
            severity=LoopSeverity.ERROR if failed_steps else LoopSeverity.INFO,
            reason=(
                "workflow_steps_completed"
                if not failed_steps
                else "workflow_steps_not_completed"
            ),
            evidence={"failed_steps": [step.id for step in failed_steps]},
        ),
        WorkflowCheckResult(
            name=LoopCheckName.WORKFLOW_STEP_EVIDENCE_PRESENT,
            passed=not empty_evidence,
            severity=LoopSeverity.ERROR if empty_evidence else LoopSeverity.INFO,
            reason=(
                "workflow_step_evidence_present"
                if not empty_evidence
                else "workflow_step_evidence_missing"
            ),
            evidence={"empty_evidence_steps": empty_evidence},
        ),
        WorkflowCheckResult(
            name=LoopCheckName.WORKFLOW_CAPABILITY_EVIDENCE_PRESENT,
            passed=not missing_execution_evidence,
            severity=LoopSeverity.ERROR if missing_execution_evidence else LoopSeverity.INFO,
            reason=(
                "workflow_capability_evidence_present"
                if not missing_execution_evidence
                else "workflow_capability_evidence_missing"
            ),
            evidence={"capability_step_count": len(capability_steps), "goal_type": goal_type},
        ),
    )
    passed = all(check.passed for check in check_results)
    blocked_reason = ""
    if not passed:
        failed_check = next(check for check in check_results if not check.passed)
        blocked_reason = failed_check.reason
    output = {
        "workflow_id": workflow.id,
        "passed": passed,
        "failed_steps": [step.id for step in failed_steps],
        "empty_evidence_steps": empty_evidence,
        "capability_step_count": len(capability_steps),
        "goal_type": goal_type,
        "checker_results": [check.to_dict() for check in check_results],
    }
    return WorkflowVerificationDecision(
        passed=passed,
        phase=Phase.ENDED, governance=Governance.NONE, acceptance=Acceptance.ACCEPTED, resolution=Resolution.SUCCESS if passed else Resolution.BLOCKED,
        event_type="workflow.verified" if passed else "workflow.verifier_blocked",
        blocked_reason=blocked_reason,
        output=output,
        check_results=check_results,
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
            check_schema_version(conn, "workflows", WORKFLOW_STORE_SCHEMA_VERSION)
            conn.execute(WORKFLOWS_TABLE.ddl)
            assert_schema_exact(conn, WORKFLOWS_TABLE)
            conn.execute(WORKFLOW_STEPS_TABLE.ddl)
            assert_schema_exact(conn, WORKFLOW_STEPS_TABLE)
            conn.execute(WORKFLOW_EVENTS_TABLE.ddl)
            assert_schema_exact(conn, WORKFLOW_EVENTS_TABLE)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflows_status ON workflows(phase, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflow_steps_workflow ON workflow_steps(workflow_id, seq)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflow_events_workflow ON workflow_events(workflow_id, created_at)"
            )
            write_schema_version(conn, "workflows", WORKFLOW_STORE_SCHEMA_VERSION)

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
            phase=Phase.PAUSED, governance=Governance.AWAITING_APPROVAL, acceptance=Acceptance.NONE, resolution=Resolution.NONE,
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
                    id, objective, phase, governance, acceptance, resolution, source, peer_id, sender_id, workspace,
                    permission_ceiling, max_concurrency, total_subagent_limit,
                    risk_class, estimated_cost, stop_condition, verification_strategy,
                    plan_json, evidence_json, blocked_reason, created_at, updated_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow.id,
                    workflow.objective,
                    workflow.phase,
                    workflow.governance,
                    workflow.acceptance,
                    workflow.resolution,
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
                        id, workflow_id, seq, role, objective, phase, governance, acceptance, resolution, depends_on_json,
                        allowed_tools_json, tool_calls_json, evidence_json, error,
                        started_at, updated_at, completed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        step.id,
                        step.workflow_id,
                        step.seq,
                        step.role,
                        step.objective,
                        step.phase,
                        step.governance,
                        step.acceptance,
                        step.resolution,
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
            workflow.id, "workflow.proposed", phase=workflow.phase, governance=workflow.governance, acceptance=workflow.acceptance, resolution=workflow.resolution, evidence=evidence or {}
        )
        return workflow

    def get(self, workflow_id: str) -> Workflow | None:
        with connect(self.db_path) as conn:
            row = conn.execute(_SELECT_WORKFLOW + " WHERE id = ?", (workflow_id,)).fetchone()
        return Workflow(*row) if row else None

    def list(self, *, phase: str = "", limit: int = 50) -> typing.List["Workflow"]:
        if phase:
            query = _SELECT_WORKFLOW + " WHERE phase = ? ORDER BY updated_at DESC LIMIT ?"
            params: tuple[Any, ...] = (phase, limit)
        else:
            query = _SELECT_WORKFLOW + " ORDER BY updated_at DESC LIMIT ?"
            params = (limit,)
        with connect(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [Workflow(*row) for row in rows]

    def list_steps(self, workflow_id: str) -> typing.List["WorkflowStep"]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                _SELECT_STEP + " WHERE workflow_id = ? ORDER BY seq ASC", (workflow_id,)
            ).fetchall()
        return [WorkflowStep(*row) for row in rows]

    def list_events(self, workflow_id: str, *, limit: int = 200) -> typing.List["WorkflowEvent"]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                _SELECT_EVENT + " WHERE workflow_id = ? ORDER BY created_at ASC LIMIT ?",
                (workflow_id, limit),
            ).fetchall()
        return [WorkflowEvent(*row) for row in rows]

    def update_state(
        self,
        workflow_id: str,
        *,
        phase: str | None = None,
        governance: str | None = None,
        acceptance: str | None = None,
        resolution: str | None = None,
        blocked_reason: str = "",
        evidence: dict[str, Any] | None = None,
        event_type: str = "workflow.phase",
    ) -> Workflow | None:
        workflow = self.get(workflow_id)
        if workflow is None:
            return None
        phase = phase if phase is not None else workflow.phase
        governance = governance if governance is not None else workflow.governance
        acceptance = acceptance if acceptance is not None else workflow.acceptance
        resolution = resolution if resolution is not None else workflow.resolution
        merged = _merge_evidence(workflow.evidence_json, evidence)
        now = time.time()
        completed_at = (
            now
            if phase == Phase.ENDED
            else workflow.completed_at
        )
        if resolution == Resolution.SUCCESS and not completed_at:
            completed_at = now
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE workflows
                SET phase = ?, governance = ?, acceptance = ?, resolution = ?, blocked_reason = ?, evidence_json = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    phase,
                    governance,
                    acceptance,
                    resolution,
                    blocked_reason,
                    json.dumps(merged, ensure_ascii=False, sort_keys=True),
                    now,
                    completed_at,
                    workflow_id,
                ),
            )
        self.record_event(workflow_id, event_type, phase=phase, governance=governance, acceptance=acceptance, resolution=resolution, evidence=evidence or {})
        return self.get(workflow_id)

    def update_step(
        self,
        step_id: str,
        *,
        phase: str | None = None,
        governance: str | None = None,
        acceptance: str | None = None,
        resolution: str | None = None,
        evidence: dict[str, Any] | None = None,
        error: str = "",
    ) -> WorkflowStep | None:
        step = self.get_step(step_id)
        if step is None:
            return None
        phase = phase if phase is not None else step.phase
        governance = governance if governance is not None else step.governance
        acceptance = acceptance if acceptance is not None else step.acceptance
        resolution = resolution if resolution is not None else step.resolution
        merged = _merge_evidence(step.evidence_json, evidence)
        now = time.time()
        started_at = (
            now if step.started_at == 0.0 and phase == Phase.RUNNING else step.started_at
        )
        completed_at = (
            now
            if phase == Phase.ENDED
            else step.completed_at
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE workflow_steps
                SET phase = ?, governance = ?, acceptance = ?, resolution = ?, evidence_json = ?, error = ?, started_at = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    phase,
                    governance,
                    acceptance,
                    resolution,
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
            f"workflow.step.{phase}_{resolution}",
            phase=phase,
            governance=governance,
            acceptance=acceptance,
            resolution=resolution,
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
        phase: str,
        governance: str,
        acceptance: str,
        resolution: str,
        step_id: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> WorkflowEvent:
        event = WorkflowEvent(
            id=uuid.uuid4().hex,
            workflow_id=workflow_id,
            event_type=event_type,
            phase=phase,
            governance=governance,
            acceptance=acceptance,
            resolution=resolution,
            step_id=step_id,
            evidence_json=json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
            created_at=time.time(),
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO workflow_events(id, workflow_id, event_type, phase, governance, acceptance, resolution, step_id, evidence_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.workflow_id,
                    event.event_type,
                    event.phase,
                    event.governance,
                    event.acceptance,
                    event.resolution,
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
        "pending_count": len([step for step in steps if step.phase == Phase.PENDING]),
        "completed_count": len([step for step in steps if step.phase == Phase.ENDED and step.resolution == Resolution.SUCCESS]),
        "failed_count": len([step for step in steps if step.phase == Phase.ENDED and step.resolution in {Resolution.FAILED, Resolution.BLOCKED}]),
    }


def _workflow_dict(workflow: Workflow) -> dict[str, Any]:
    data = workflow.__dict__.copy()
    data["plan"] = _loads(workflow.plan_json, {})
    data["evidence"] = _loads(workflow.evidence_json, {})
    return data


def _step_dict(step: WorkflowStep) -> dict[str, Any]:
    data = step.__dict__.copy()
    data["depends_on"] = _loads(step.depends_on_json, []) or []
    data["allowed_tools"] = _loads(step.allowed_tools_json, []) or []
    data["tool_calls"] = _loads(step.tool_calls_json, []) or []
    data["evidence"] = _loads(step.evidence_json, {})
    return data


def _event_dict(event: WorkflowEvent) -> dict[str, Any]:
    data = event.__dict__.copy()
    data["evidence"] = _loads(event.evidence_json, {})
    return data


def _normalize_steps(
    raw_steps: list[dict[str, Any]], *, workflow_id: str, total_limit: int
) -> typing.List["WorkflowStep"]:
    steps: list["WorkflowStep"] = []
    now = time.time()
    for index, raw in enumerate(raw_steps[: max(1, min(int(total_limit or 32), 1000))], start=1):
        if not isinstance(raw, dict):
            continue
        objective = str(raw.get("objective") or raw.get("prompt") or "").strip()
        if not objective:
            continue
        role = str(raw.get("role") or "worker").strip() or "worker"
        raw_depends_on = raw.get("depends_on")
        depends_on = raw_depends_on if isinstance(raw_depends_on, list) else []
        raw_allowed_tools = raw.get("allowed_tools")
        allowed_tools = raw_allowed_tools if isinstance(raw_allowed_tools, list) else []
        raw_tool_calls = raw.get("tool_calls")
        tool_calls = raw_tool_calls if isinstance(raw_tool_calls, list) else []
        steps.append(
            WorkflowStep(
                id=str(raw.get("id") or uuid.uuid4().hex),
                workflow_id=workflow_id,
                seq=int(raw.get("seq") or index),
                role=role[:64],
                objective=objective,
                phase=Phase.PENDING, governance=Governance.NONE, acceptance=Acceptance.NONE, resolution=Resolution.NONE,
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


def _json_list(value: str) -> typing.List[Any]:
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
        Column("phase", "TEXT", nullable=False),
        Column("governance", "TEXT", nullable=False),
        Column("acceptance", "TEXT", nullable=False),
        Column("resolution", "TEXT", nullable=False),
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
        Column("phase", "TEXT", nullable=False),
        Column("governance", "TEXT", nullable=False),
        Column("acceptance", "TEXT", nullable=False),
        Column("resolution", "TEXT", nullable=False),
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
        Column("phase", "TEXT", nullable=False),
        Column("governance", "TEXT", nullable=False),
        Column("acceptance", "TEXT", nullable=False),
        Column("resolution", "TEXT", nullable=False),
        Column("step_id", "TEXT", nullable=False),
        Column("evidence_json", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
    ],
)
_SELECT_WORKFLOW = """
    SELECT id, objective, phase, governance, acceptance, resolution, source, peer_id, sender_id, workspace,
           permission_ceiling, max_concurrency, total_subagent_limit,
           risk_class, estimated_cost, stop_condition, verification_strategy,
           plan_json, evidence_json, blocked_reason, created_at, updated_at, completed_at
    FROM workflows
"""
_SELECT_STEP = """
    SELECT id, workflow_id, seq, role, objective, phase, governance, acceptance, resolution, depends_on_json,
           allowed_tools_json, tool_calls_json, evidence_json, error,
           started_at, updated_at, completed_at
    FROM workflow_steps
"""
_SELECT_EVENT = """
    SELECT id, workflow_id, event_type, phase, governance, acceptance, resolution, step_id, evidence_json, created_at
    FROM workflow_events
"""
