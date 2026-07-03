"""Workflow domain types, status constants, and pure transition logic."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..loop import LoopCheckName, LoopSeverity

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
STEP_TERMINAL_STATUSES = frozenset(
    {
        STEP_STATUS_COMPLETED,
        STEP_STATUS_FAILED,
        STEP_STATUS_BLOCKED,
    }
)
STEP_FAILED_STATUSES = frozenset(
    {
        STEP_STATUS_FAILED,
        STEP_STATUS_BLOCKED,
    }
)

WORKFLOW_RUNNABLE_STATUSES = frozenset(
    {
        WORKFLOW_STATUS_APPROVED,
        WORKFLOW_STATUS_RUNNING,
        WORKFLOW_STATUS_INTERRUPTED,
    }
)


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
            blocked_reason="workflow_step_failed",
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
    status_completed = workflow.status in (
        WORKFLOW_STATUS_COMPLETED,
        WORKFLOW_STATUS_VERIFIED_COMPLETE,
    )
    check_results = (
        WorkflowCheckResult(
            name=LoopCheckName.WORKFLOW_STATUS_COMPLETED,
            passed=status_completed,
            severity=LoopSeverity.ERROR if not status_completed else LoopSeverity.INFO,
            reason=(
                "workflow_status_completed"
                if status_completed
                else "workflow_status_not_completed"
            ),
            evidence={"status": workflow.status},
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
        status=WORKFLOW_STATUS_VERIFIED_COMPLETE if passed else WORKFLOW_STATUS_BLOCKED,
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
