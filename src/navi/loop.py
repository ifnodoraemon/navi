from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class LoopPhase(StrEnum):
    DECISION = "loop.decision"
    CHECK = "loop.check"
    RECOVERY = "loop.recovery"
    RUNTIME = "runtime"
    PLANNER = "planner"
    CAPABILITY = "capability"
    WORKFLOW_STEP = "workflow.step"
    WORKFLOW_VERIFY = "workflow.verify"


class LoopDecisionKind(StrEnum):
    CONTINUE = "continue"
    RECOVER = "recover"
    PAUSE_FOR_APPROVAL = "pause_for_approval"
    CONVERGED = "converged"
    FINALIZE = "finalize"
    BLOCKED = "blocked"
    FAILED = "failed"


class LoopReason(StrEnum):
    APPROVAL_ALREADY_PENDING = "approval_already_pending"
    APPROVAL_REQUIRED = "approval_required"
    CAPABILITY_FAILURE = "capability_failure"
    CAPABILITY_OBSERVATION_APPENDED = "capability_observation_appended"
    COMPLETION_CHECKER_BLOCKED = "completion_checker_blocked"
    COMPLETION_EVIDENCE_TRUE = "completion_evidence_true"
    PLANNER_OR_PARSER_FAILURE = "planner_or_parser_failure"
    PROVIDER_NO_RESPONSE = "provider_no_response"
    REPEATED_PROGRESS_SIGNATURE = "repeated_progress_signature"
    REPEATED_RECOVERY_SIGNATURE = "repeated_recovery_signature"
    TERMINAL_RESULT = "terminal_result"
    WORKFLOW_STEP_CAPABILITY_FAILURE = "workflow_step_capability_failure"
    WORKFLOW_STEP_COMPLETED = "workflow_step_completed"
    WORKFLOW_STEP_REQUESTED_USER_INPUT = "workflow_step_requested_user_input"
    WORKFLOW_VERIFIER_BLOCKED = "workflow_verifier_blocked"
    WORKFLOW_VERIFIER_PASSED = "workflow_verifier_passed"


class LoopCheckName(StrEnum):
    APPROVAL_GATE = "approval_gate"
    CAPABILITY_RESULT = "capability_result"
    COMPLETION_CHECKER = "completion_checker"
    COMPLETION_EVIDENCE = "completion_evidence"
    NO_PROGRESS_GATE = "no_progress_gate"
    PLANNER_RESULT = "planner_result"
    TERMINAL_RESULT = "terminal_result"
    WORKFLOW_CAPABILITY_EVIDENCE_PRESENT = "workflow_capability_evidence_present"
    WORKFLOW_STATUS_COMPLETED = "workflow_status_completed"
    WORKFLOW_STEP_CHECKER = "workflow_step_checker"
    WORKFLOW_STEP_EVIDENCE_PRESENT = "workflow_step_evidence_present"
    WORKFLOW_STEPS_COMPLETED = "workflow_steps_completed"


class LoopNextAction(StrEnum):
    BLOCK_WORKFLOW = "block_workflow"
    BLOCK_WORKFLOW_STEP = "block_workflow_step"
    COMPLETE_STEP = "complete_step"
    CONTINUE = "continue"
    FAIL_WORKFLOW_STEP = "fail_workflow_step"
    FINALIZE_STABLE_OBSERVATIONS = "finalize_stable_observations"
    MARK_WORKFLOW_VERIFIED = "mark_workflow_verified"
    PLAN_NEXT_STEP = "plan_next_step"
    WAIT_FOR_APPROVAL = "wait_for_approval"


class TracePhase(StrEnum):
    AGENT_ROLE_RESULT = "agent.role_result"
    CAPABILITY_RESULT = "capability.result"
    PLANNER_CALL_ERROR = "planner.call.error"
    PLANNER_CALL_START = "planner.call.start"
    PLANNER_PARSE_ERROR = "planner.parse_error"
    PLANNER_SYSCALL = "planner.syscall"
    RUNTIME_CONVERGED = "runtime.converged"
    TURN_FINAL = "turn.final"
    TURN_START = "turn.start"


class TraceOutcome(StrEnum):
    SUCCESS = "success"
    DEGRADED = "degraded"
    FAILURE = "failure"
    UNKNOWN = "unknown"


class TraceFailureDomain(StrEnum):
    APPROVAL_LOOP = "approval_loop"
    CAPABILITY_FAILURE = "capability_failure"
    CHECKER_BLOCKED = "checker_blocked"
    LOOP_NO_PROGRESS = "loop_no_progress"
    MISSING_COMPLETION_CHECK = "missing_completion_check"
    NONE = "none"
    PLANNER_OR_PARSER = "planner_or_parser"
    PROVIDER_NO_RESPONSE = "provider_no_response"
    RUNTIME = "runtime"
    SAFEGUARD_POLICY = "safeguard_policy"
    TRACE_MISSING = "trace_missing"


class LoopSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class LoopCheckResult:
    name: str
    passed: bool
    severity: str = LoopSeverity.INFO
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


@dataclass(frozen=True)
class LoopDecision:
    decision: str
    reason: str
    phase: str = ""
    tool: str = ""
    run_id: str = ""
    workflow_id: str = ""
    step_id: str = ""
    goal_ids: tuple[str, ...] = ()
    progress_signature: str = ""
    checker_results: tuple[LoopCheckResult, ...] = ()
    gate_results: tuple[LoopCheckResult, ...] = ()
    next_action: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "phase": self.phase,
            "tool": self.tool,
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "step_id": self.step_id,
            "goal_ids": list(self.goal_ids),
            "progress_signature": self.progress_signature,
            "checker_results": [item.to_dict() for item in self.checker_results],
            "gate_results": [item.to_dict() for item in self.gate_results],
            "next_action": self.next_action,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class TraceRunView:
    id: str
    trace_id: str
    parent_run_id: str
    name: str
    run_type: str
    status: str
    start_time: float
    end_time: float
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "trace_id": self.trace_id,
            "parent_run_id": self.parent_run_id,
            "name": self.name,
            "run_type": self.run_type,
            "status": self.status,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "tags": list(self.tags),
            "metadata": self.metadata,
        }


NON_OK_LOOP_DECISIONS = frozenset(
    {LoopDecisionKind.BLOCKED, LoopDecisionKind.FAILED}
)
APPROVAL_LOOP_DECISIONS = frozenset(
    {LoopDecisionKind.PAUSE_FOR_APPROVAL, LoopDecisionKind.CONTINUE}
)

LOOP_FAILURE_DOMAIN_RULES: tuple[tuple[TraceFailureDomain, tuple[str, ...]], ...] = (
    (TraceFailureDomain.PROVIDER_NO_RESPONSE, ("provider", "response")),
    (TraceFailureDomain.PLANNER_OR_PARSER, ("planner", "parse", "parser")),
    (TraceFailureDomain.SAFEGUARD_POLICY, ("safeguard", "policy")),
    (TraceFailureDomain.CAPABILITY_FAILURE, ("capability", "tool")),
    (TraceFailureDomain.CHECKER_BLOCKED, ("checker", "completion")),
)
LOOP_BLOCKED_DOMAIN_RULES: tuple[tuple[TraceFailureDomain, tuple[str, ...]], ...] = (
    (TraceFailureDomain.APPROVAL_LOOP, ("approval",)),
    (TraceFailureDomain.SAFEGUARD_POLICY, ("safeguard", "policy")),
    (TraceFailureDomain.CAPABILITY_FAILURE, ("capability", "tool")),
)
APPROVAL_LOOP_TOKENS = frozenset({"already", "duplicate", "repeated", "pending"})


def loop_decision_ok(decision: LoopDecision) -> bool:
    return str(decision.decision) not in {str(item) for item in NON_OK_LOOP_DECISIONS}


def failed_loop_result_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if not isinstance(item, dict) or item.get("passed") is not False:
            continue
        name = str(item.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def loop_reason_text(output: dict[str, Any]) -> str:
    parts = [
        str(output.get("decision") or ""),
        str(output.get("reason") or ""),
        str(output.get("next_action") or ""),
        " ".join(failed_loop_result_names(output.get("checker_results"))),
        " ".join(failed_loop_result_names(output.get("gate_results"))),
    ]
    return " ".join(parts).lower()


def classify_loop_failure(output: dict[str, Any]) -> TraceFailureDomain:
    return _classify_by_tokens(
        loop_reason_text(output),
        LOOP_FAILURE_DOMAIN_RULES,
        default=TraceFailureDomain.RUNTIME,
    )


def classify_loop_blocked(output: dict[str, Any]) -> TraceFailureDomain:
    return _classify_by_tokens(
        loop_reason_text(output),
        LOOP_BLOCKED_DOMAIN_RULES,
        default=TraceFailureDomain.CHECKER_BLOCKED,
    )


def is_approval_loop_decision(output: dict[str, Any]) -> bool:
    if str(output.get("decision") or "") not in {str(item) for item in APPROVAL_LOOP_DECISIONS}:
        return False
    text = loop_reason_text(output)
    return "approval" in text and any(token in text for token in APPROVAL_LOOP_TOKENS)


def _classify_by_tokens(
    text: str,
    rules: tuple[tuple[TraceFailureDomain, tuple[str, ...]], ...],
    *,
    default: TraceFailureDomain,
) -> TraceFailureDomain:
    for domain, tokens in rules:
        if any(token in text for token in tokens):
            return domain
    return default
