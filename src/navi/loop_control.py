from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable

from .capability_contract import CAPABILITY_ERROR_REASON_KEY
from .engine_types import AgentTurnResult
from .lifecycle import RUN_STATUS_AWAITING_APPROVAL, RUN_STATUS_COMPLETED, RUN_STATUS_FAILED
from .loop import (
    LoopCheckName,
    LoopCheckResult,
    LoopDecision,
    LoopDecisionKind,
    LoopPhase,
    LoopProgressGate,
    LoopReason,
    LoopSeverity,
    TraceFailureDomain,
    trace_failure_domain,
)


class LoopControlEffect(StrEnum):
    CONTINUE_LOOP = "continue_loop"
    FINALIZE_STABLE = "finalize_stable"


@dataclass(frozen=True)
class LoopControlResult:
    effect: LoopControlEffect
    decisions: tuple[LoopDecision, ...]
    progress_signature: str = ""
    convergence_message: str = ""
    runtime_observation: str = ""


@dataclass(frozen=True)
class RuntimeStepFrame:
    result: AgentTurnResult
    facts: dict[str, Any] | None
    tool: str
    progress_signature: str
    goal_ids: set[str]
    observations_count: int


@dataclass(frozen=True)
class RecoveryStepFrame(RuntimeStepFrame):
    recovery_observation: str = ""





def reduce_recovery_step(
    frame: RecoveryStepFrame,
    *,
    progress_gate: LoopProgressGate,
) -> LoopControlResult:
    progress = progress_gate.observe(frame.progress_signature)
    recovery = LoopDecision(
        decision=LoopDecisionKind.RECOVER,
        reason=LoopReason.COMPLETION_CHECKER_BLOCKED,
        phase=LoopPhase.CHECK,
        failure_domain=TraceFailureDomain.CHECKER_BLOCKED,
        tool=frame.tool,
        run_id=frame.result.run_id,
        progress_signature=progress.signature,
        checker_results=(
            LoopCheckResult(
                name=LoopCheckName.COMPLETION_CHECKER,
                passed=False,
                severity=LoopSeverity.ERROR,
                reason=LoopReason.COMPLETION_CHECKER_BLOCKED,
                evidence={"recovery_observation": frame.recovery_observation},
            ),
        ),
    )
    if progress.count == 4:
        return LoopControlResult(
            effect=LoopControlEffect.CONTINUE_LOOP,
            decisions=(recovery,),
            progress_signature=progress.signature,
            runtime_observation="repeated_action_limit_reached",
        )

    if progress.repeated:
        return LoopControlResult(
            effect=LoopControlEffect.FINALIZE_STABLE,
            decisions=(
                recovery,
                _converged_decision(
                    frame,
                    reason=LoopReason.REPEATED_RECOVERY_SIGNATURE,
                    progress_signature=progress.signature,
                ),
            ),
            progress_signature=progress.signature,
            convergence_message="repeated_action_limit_reached",
        )
    return LoopControlResult(
        effect=LoopControlEffect.CONTINUE_LOOP,
        decisions=(recovery,),
        progress_signature=progress.signature,
    )


def reduce_runtime_step(
    frame: RuntimeStepFrame,
    *,
    progress_gate: LoopProgressGate,
) -> LoopControlResult:
    if _facts_waiting_for_approval(frame.facts):
        pause_decision = LoopDecision(
            decision=LoopDecisionKind.PAUSE_FOR_APPROVAL,
            reason=LoopReason.APPROVAL_REQUIRED,
            phase=LoopPhase.PLANNER,
            failure_domain=TraceFailureDomain.NONE,
            tool=frame.tool,
            run_id=frame.result.run_id,
            evidence=frame.facts,
            goal_ids=tuple(frame.goal_ids),
            gate_results=(
                LoopCheckResult(
                    name=LoopCheckName.APPROVAL_GATE,
                    passed=True,
                    severity=LoopSeverity.INFO,
                    reason=LoopReason.APPROVAL_REQUIRED,
                    evidence=frame.facts or {},
                ),
            ),
        )
        return LoopControlResult(
            effect=LoopControlEffect.FINALIZE_STABLE,
            decisions=(pause_decision,),
            progress_signature=frame.progress_signature,
        )

    if frame.result.ok and _facts_complete_current_request(frame.facts):
        return LoopControlResult(
            effect=LoopControlEffect.FINALIZE_STABLE,
            decisions=(_completion_evidence_decision(frame),),
            progress_signature=frame.progress_signature,
        )

    progress = progress_gate.observe(frame.progress_signature)
    if progress.count == 4:
        return LoopControlResult(
            effect=LoopControlEffect.CONTINUE_LOOP,
            decisions=(),
            progress_signature=progress.signature,
            runtime_observation="repeated_action_limit_reached",
        )

    if progress.repeated:
        return LoopControlResult(
            effect=LoopControlEffect.FINALIZE_STABLE,
            decisions=(
                _converged_decision(
                    frame,
                    reason=LoopReason.REPEATED_PROGRESS_SIGNATURE,
                    progress_signature=progress.signature,
                ),
            ),
            progress_signature=progress.signature,
            convergence_message="repeated_action_limit_reached",
        )

    return LoopControlResult(
        effect=LoopControlEffect.CONTINUE_LOOP,
        decisions=(_continue_decision(frame, progress_signature=progress.signature),),
        progress_signature=progress.signature,
    )


def failure_decision_for_return(result: AgentTurnResult, *, tool: str) -> LoopDecision:
    text = result.text or result.observation
    reason = LoopReason.PLANNER_OR_PARSER_FAILURE
    failure_domain = trace_failure_domain((result.facts or {}).get("failure_domain"))
    domain = TraceFailureDomain(failure_domain) if failure_domain else TraceFailureDomain.PLANNER_OR_PARSER
    if domain == TraceFailureDomain.PROVIDER_NO_RESPONSE:
        reason = LoopReason.PROVIDER_NO_RESPONSE
    return LoopDecision(
        decision=LoopDecisionKind.FAILED,
        reason=reason,
        phase=LoopPhase.PLANNER,
        failure_domain=domain,
        tool=tool or result.action,
        run_id=result.run_id,
        checker_results=(
            LoopCheckResult(
                name=LoopCheckName.PLANNER_RESULT,
                passed=False,
                severity=LoopSeverity.ERROR,
                reason=reason,
                evidence={"error_message": text} if text else {},
            ),
        ),
    )


def terminal_loop_decision(
    result: AgentTurnResult,
    facts: dict[str, Any] | None,
    *,
    tool: str,
    goal_ids: set[str],
) -> LoopDecision:
    return _terminal_result_decision(result, facts, goal_ids, tool)


def workflow_step_loop_decision(
    result: AgentTurnResult,
    *,
    workflow_id: str,
    step_id: str,
) -> LoopDecision:
    for builder in WORKFLOW_STEP_DECISION_BUILDERS:
        decision = builder(result, workflow_id, step_id)
        if decision is not None:
            return decision
    return _workflow_step_completed_decision(result, workflow_id, step_id)


def workflow_step_block_reason(result: AgentTurnResult) -> str:
    if getattr(result, "yields_control", False):
        return getattr(result, "error_reason", "") or "user_input_requested"
    if not getattr(result, "ok", True):
        return getattr(result, "error_reason", "") or (result.facts or {}).get("error_reason", "")
    return ""


def workflow_verification_loop_decision(
    *,
    workflow_id: str,
    passed: bool,
    check_results: Any,
    output: dict[str, Any],
) -> LoopDecision:
    return LoopDecision(
        decision=LoopDecisionKind.FINALIZE if passed else LoopDecisionKind.BLOCKED,
        reason=LoopReason.WORKFLOW_VERIFIER_PASSED
        if passed
        else LoopReason.WORKFLOW_VERIFIER_BLOCKED,
        phase=LoopPhase.WORKFLOW_VERIFY,
        failure_domain=TraceFailureDomain.NONE if passed else TraceFailureDomain.CHECKER_BLOCKED,
        tool="workflow.run",
        run_id=workflow_id,
        workflow_id=workflow_id,
        checker_results=tuple(
            LoopCheckResult(
                name=check.name,
                passed=check.passed,
                severity=check.severity,
                reason=check.reason,
                evidence=check.evidence,
            )
            for check in check_results
        ),
        evidence=output,
    )


def semantic_progress_signature(
    tool: str,
    args: dict[str, Any],
    *,
    ok: bool,
    facts: dict[str, Any] | None,
) -> str:
    payload: dict[str, Any] = {
        "tool": tool,
        "ok": ok,
        "facts": facts or {},
    }
    if not facts:
        payload["args"] = args
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _completion_evidence_decision(frame: RuntimeStepFrame) -> LoopDecision:
    return LoopDecision(
        decision=LoopDecisionKind.FINALIZE,
        reason=LoopReason.COMPLETION_EVIDENCE_TRUE,
        phase=LoopPhase.RUNTIME,
        tool=frame.tool,
        run_id=frame.result.run_id,
        goal_ids=tuple(sorted(frame.goal_ids)),
        checker_results=(
            LoopCheckResult(
                name=LoopCheckName.COMPLETION_EVIDENCE,
                passed=True,
                reason=LoopReason.COMPLETION_EVIDENCE_TRUE,
            ),
        ),
    )


def _continue_decision(
    frame: RuntimeStepFrame,
    *,
    progress_signature: str,
) -> LoopDecision:
    return LoopDecision(
        decision=LoopDecisionKind.CONTINUE,
        reason=LoopReason.CAPABILITY_OBSERVATION_APPENDED,
        phase=LoopPhase.RUNTIME,
        tool=frame.tool,
        run_id=frame.result.run_id,
        progress_signature=progress_signature,
        goal_ids=tuple(sorted(frame.goal_ids)),
        checker_results=(
            LoopCheckResult(
                name=LoopCheckName.COMPLETION_EVIDENCE,
                passed=False,
                severity=LoopSeverity.INFO,
                reason=LoopReason.CAPABILITY_OBSERVATION_APPENDED,
            ),
        ),
    )


def _converged_decision(
    frame: RuntimeStepFrame,
    *,
    reason: LoopReason,
    progress_signature: str,
) -> LoopDecision:
    return LoopDecision(
        decision=LoopDecisionKind.CONVERGED,
        reason=reason,
        phase=LoopPhase.RUNTIME,
        failure_domain=TraceFailureDomain.LOOP_NO_PROGRESS,
        tool=frame.tool,
        run_id=frame.result.run_id,
        progress_signature=progress_signature,
        goal_ids=tuple(sorted(frame.goal_ids)),
        gate_results=(
            LoopCheckResult(
                name=LoopCheckName.NO_PROGRESS_GATE,
                passed=False,
                severity=LoopSeverity.WARNING,
                reason=reason,
                evidence={
                    "observations_count": frame.observations_count,
                    "progress_signature": progress_signature,
                },
            ),
        ),
    )






def _terminal_result_decision(
    result: AgentTurnResult,
    facts: dict[str, Any] | None,
    goal_ids: set[str],
    tool: str,
) -> LoopDecision:
    del facts
    return LoopDecision(
        decision=LoopDecisionKind.FINALIZE,
        reason=LoopReason.TERMINAL_RESULT,
        phase=LoopPhase.RUNTIME,
        tool=tool or result.action,
        run_id=result.run_id,
        goal_ids=tuple(sorted(goal_ids)),
        checker_results=(
            LoopCheckResult(
                name=LoopCheckName.TERMINAL_RESULT,
                passed=True,
                reason=LoopReason.TERMINAL_RESULT,
                evidence={"terminal_action": result.action},
            ),
        ),
    )





WorkflowStepDecisionBuilder = Callable[[AgentTurnResult, str, str], LoopDecision | None]


def _workflow_step_user_input_decision(
    result: AgentTurnResult,
    workflow_id: str,
    step_id: str,
) -> LoopDecision | None:
    if workflow_step_block_reason(result) != str(LoopReason.WORKFLOW_STEP_REQUESTED_USER_INPUT):
        return None
    return _workflow_step_decision(
        result,
        workflow_id=workflow_id,
        step_id=step_id,
        decision=LoopDecisionKind.BLOCKED,
        reason=LoopReason.WORKFLOW_STEP_REQUESTED_USER_INPUT,
        failure_domain=TraceFailureDomain.CHECKER_BLOCKED,
        check_passed=False,
        severity=LoopSeverity.ERROR,
    )


def _workflow_step_capability_failure_decision(
    result: AgentTurnResult,
    workflow_id: str,
    step_id: str,
) -> LoopDecision | None:
    error_reason = workflow_step_block_reason(result)
    if not error_reason:
        return None
    return _workflow_step_decision(
        result,
        workflow_id=workflow_id,
        step_id=step_id,
        decision=LoopDecisionKind.FAILED,
        reason=LoopReason.WORKFLOW_STEP_CAPABILITY_FAILURE,
        failure_domain=TraceFailureDomain.CAPABILITY_FAILURE,
        check_passed=False,
        severity=LoopSeverity.ERROR,
        check_reason=error_reason or str(LoopReason.WORKFLOW_STEP_CAPABILITY_FAILURE),
    )


def _workflow_step_completed_decision(
    result: AgentTurnResult,
    workflow_id: str,
    step_id: str,
) -> LoopDecision:
    return _workflow_step_decision(
        result,
        workflow_id=workflow_id,
        step_id=step_id,
        decision=LoopDecisionKind.FINALIZE,
        reason=LoopReason.WORKFLOW_STEP_COMPLETED,
        failure_domain=TraceFailureDomain.NONE,
        check_passed=True,
        severity=LoopSeverity.INFO,
    )


def _workflow_step_decision(
    result: AgentTurnResult,
    *,
    workflow_id: str,
    step_id: str,
    decision: LoopDecisionKind,
    reason: LoopReason,
    failure_domain: TraceFailureDomain,
    check_passed: bool,
    severity: LoopSeverity,
    check_reason: str = "",
) -> LoopDecision:
    return LoopDecision(
        decision=decision,
        reason=reason,
        phase=LoopPhase.WORKFLOW_STEP,
        failure_domain=failure_domain,
        tool=result.action,
        run_id=result.run_id,
        workflow_id=workflow_id,
        step_id=step_id,
        checker_results=(
            LoopCheckResult(
                name=LoopCheckName.WORKFLOW_STEP_CHECKER,
                passed=check_passed,
                severity=severity,
                reason=check_reason or str(reason),
            ),
        ),
    )


WORKFLOW_STEP_DECISION_BUILDERS: tuple[WorkflowStepDecisionBuilder, ...] = (
    _workflow_step_user_input_decision,
    _workflow_step_capability_failure_decision,
)


def _facts_complete_current_request(facts: dict[str, Any] | None) -> bool:
    if not isinstance(facts, dict):
        return False
    if facts.get("completion_evidence") is True:
        return True
    # Only `completed` (success) and `failed` (terminal) mark the current
    # request as done. `pending` / `running` are in-progress states that must
    # not prematurely finalize the loop.
    status = str(facts.get("status") or facts.get("run_status") or "").strip()
    if status not in (RUN_STATUS_COMPLETED, RUN_STATUS_FAILED):
        return False
    return (
        str(facts.get("turn_scope") or "").strip() == "current"
        and bool(str(facts.get("state_transition") or "").strip())
        and bool(str(facts.get("entity_type") or "").strip())
        and bool(str(facts.get("entity_id") or "").strip())
    )


def _facts_waiting_for_approval(facts: dict[str, Any] | None) -> bool:
    if not isinstance(facts, dict):
        return False
    status = str(facts.get("status") or facts.get("run_status") or "").strip()
    if status == RUN_STATUS_AWAITING_APPROVAL:
        return True
    approval = facts.get("approval")
    if isinstance(approval, dict) and (
        approval.get("status") == "pending" or str(approval.get("code") or "").strip()
    ):
        return True
    approval_resolution = facts.get("approval_resolution")
    return isinstance(approval_resolution, dict) and approval_resolution.get("reason") == "approval_pending"


def _capability_error_is_input_schema_mismatch(facts: dict[str, Any] | None) -> bool:
    return (
        isinstance(facts, dict)
        and facts.get(CAPABILITY_ERROR_REASON_KEY) == "schema_mismatch"
        and "result_action" not in facts
    )
