from __future__ import annotations
from navi.lifecycle import Phase, Resolution

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .capability_contract import CAPABILITY_ERROR_REASON_KEY
from .engine_types import AgentTurnResult
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
    output_signature: str
    goal_ids: set[str]
    observations_count: int


@dataclass(frozen=True)
class RecoveryStepFrame(RuntimeStepFrame):
    recovery_observation: str = ""





def reduce_recovery_step(
    frame: RecoveryStepFrame,
    *,
    progress_gate: LoopProgressGate,
    output_progress_gate: LoopProgressGate,
) -> LoopControlResult:
    progress = progress_gate.observe(frame.progress_signature, tool=frame.tool)
    output_progress = output_progress_gate.observe(frame.output_signature, tool=frame.tool)
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
    if progress.repeated:
        prefix = "repeated_recovery"
        if progress.reason == "chain_repeated":
            prefix = "chain_repeated_recovery"
        elif progress.reason == "tool_repeated":
            prefix = "tool_repeated_recovery"

        return LoopControlResult(
            effect=LoopControlEffect.CONTINUE_LOOP,
            decisions=(recovery,),
            progress_signature=progress.signature,
            runtime_observation=_get_escalating_observation(
                prefix,
                progress.count,
                "attempted",
                tool=frame.tool,
                progress_signature=progress.signature,
            ),
        )
    if output_progress.repeated:
        prefix = "repeated_output"
        if output_progress.reason == "chain_repeated":
            prefix = "chain_repeated_output"
        elif output_progress.reason == "tool_repeated":
            prefix = "tool_repeated_output"

        return LoopControlResult(
            effect=LoopControlEffect.CONTINUE_LOOP,
            decisions=(recovery,),
            progress_signature=progress.signature,
            runtime_observation=_get_escalating_observation(
                prefix,
                output_progress.count,
                "generated",
                tool=frame.tool,
                progress_signature=output_progress.signature,
            ),
        )
    return LoopControlResult(
        effect=LoopControlEffect.CONTINUE_LOOP,
        decisions=(recovery,),
        progress_signature=progress.signature,
    )

def _get_escalating_observation(
    prefix: str,
    count: int,
    action_type: str,
    *,
    tool: str,
    progress_signature: str,
) -> str:
    return json.dumps(
        {
            "observation_type": "loop_progress_fact",
            "facts": {
                "reason": str(LoopReason.REPEATED_PROGRESS_SIGNATURE),
                "pattern": prefix,
                "repeat_count": count,
                "action_type": action_type,
                "tool": tool,
                "progress_signature": progress_signature,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def reduce_runtime_step(
    frame: RuntimeStepFrame,
    *,
    progress_gate: LoopProgressGate,
    output_progress_gate: LoopProgressGate,
) -> LoopControlResult:
    progress = progress_gate.observe(frame.progress_signature, tool=frame.tool)
    output_progress = output_progress_gate.observe(frame.output_signature, tool=frame.tool)

    if frame.result.ok and _facts_complete_current_request(frame.facts):
        repeated = progress.repeated or output_progress.repeated
        return LoopControlResult(
            effect=(
                LoopControlEffect.FINALIZE_STABLE
                if repeated
                else LoopControlEffect.CONTINUE_LOOP
            ),
            decisions=(
                _completion_evidence_decision(
                    frame,
                    decision=(
                        LoopDecisionKind.FINALIZE
                        if repeated
                        else LoopDecisionKind.CONTINUE
                    ),
                    progress_signature=progress.signature,
                    repeated=repeated,
                ),
            ),
            progress_signature=progress.signature,
        )

    if progress.repeated:
        prefix = "repeated_action"
        if progress.reason == "chain_repeated":
            prefix = "chain_repeated_action"
        elif progress.reason == "tool_repeated":
            prefix = "tool_repeated_action"

        return LoopControlResult(
            effect=LoopControlEffect.FINALIZE_STABLE,
            decisions=(
                _repeated_progress_decision(
                    frame,
                    progress_signature=progress.signature,
                    pattern=prefix,
                    repeat_count=progress.count,
                ),
            ),
            progress_signature=progress.signature,
        )

    if output_progress.repeated:
        prefix = "repeated_output"
        if output_progress.reason == "chain_repeated":
            prefix = "chain_repeated_output"
        elif output_progress.reason == "tool_repeated":
            prefix = "tool_repeated_output"

        return LoopControlResult(
            effect=LoopControlEffect.FINALIZE_STABLE,
            decisions=(
                _repeated_progress_decision(
                    frame,
                    progress_signature=output_progress.signature,
                    pattern=prefix,
                    repeat_count=output_progress.count,
                ),
            ),
            progress_signature=progress.signature,
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





def semantic_progress_signature(
    tool: str,
    args: dict[str, Any],
    *,
    ok: bool,
    facts: dict[str, Any] | None,
) -> str:
    stripped_facts = dict(facts) if facts else {}
    for k in ("run_id", "entity_id", "goal_id", "approval_id", "task_id"):
        stripped_facts.pop(k, None)

    payload: dict[str, Any] = {
        "tool": tool,
        "ok": ok,
        "facts": stripped_facts,
    }
    if not facts:
        payload["args"] = args
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _completion_evidence_decision(
    frame: RuntimeStepFrame,
    *,
    decision: LoopDecisionKind,
    progress_signature: str,
    repeated: bool,
) -> LoopDecision:
    return LoopDecision(
        decision=decision,
        reason=LoopReason.COMPLETION_EVIDENCE_TRUE,
        phase=LoopPhase.RUNTIME,
        tool=frame.tool,
        run_id=frame.result.run_id,
        progress_signature=progress_signature,
        goal_ids=tuple(sorted(frame.goal_ids)),
        checker_results=(
            LoopCheckResult(
                name=LoopCheckName.COMPLETION_EVIDENCE,
                passed=True,
                reason=LoopReason.COMPLETION_EVIDENCE_TRUE,
                evidence={
                    "repeated": repeated,
                    "facts": frame.facts or {},
                },
            ),
        ),
        evidence=frame.facts or {},
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


def _repeated_progress_decision(
    frame: RuntimeStepFrame,
    *,
    progress_signature: str,
    pattern: str,
    repeat_count: int,
) -> LoopDecision:
    return LoopDecision(
        decision=LoopDecisionKind.CONVERGED,
        reason=LoopReason.REPEATED_PROGRESS_SIGNATURE,
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
                reason=LoopReason.REPEATED_PROGRESS_SIGNATURE,
                evidence={
                    "observations_count": frame.observations_count,
                    "reason": str(LoopReason.REPEATED_PROGRESS_SIGNATURE),
                    "pattern": pattern,
                    "repeat_count": repeat_count,
                    "progress_signature": progress_signature,
                    "tool": frame.tool,
                    "action": frame.result.action,
                    "facts": frame.facts or {},
                },
            ),
        ),
        evidence={
            "reason": str(LoopReason.REPEATED_PROGRESS_SIGNATURE),
            "pattern": pattern,
            "repeat_count": repeat_count,
            "progress_signature": progress_signature,
            "tool": frame.tool,
            "action": frame.result.action,
            "facts": frame.facts or {},
        },
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








def _facts_complete_current_request(facts: dict[str, Any] | None) -> bool:
    if not isinstance(facts, dict):
        return False
    if facts.get("completion_evidence") is True:
        return True
    # Only an ended run with a terminal resolution marks the current request as
    # done. Pending/running/paused are in-progress states.
    phase = str(facts.get("phase") or facts.get("run_phase") or "").strip()
    resolution = str(facts.get("resolution") or facts.get("run_resolution") or "").strip()
    if phase != Phase.ENDED or resolution not in {Resolution.SUCCESS, Resolution.FAILED}:
        return False
    return (
        str(facts.get("turn_scope") or "").strip() == "current"
        and bool(str(facts.get("state_transition") or "").strip())
        and bool(str(facts.get("entity_type") or "").strip())
        and bool(str(facts.get("entity_id") or "").strip())
    )


def _capability_error_is_input_schema_mismatch(facts: dict[str, Any] | None) -> bool:
    return (
        isinstance(facts, dict)
        and facts.get(CAPABILITY_ERROR_REASON_KEY) == "schema_mismatch"
        and "result_action" not in facts
    )
