"""Trace evaluation rules."""

from __future__ import annotations
from navi.lifecycle import Phase, Governance, Acceptance, Resolution
from navi.loop import LoopPhase

from dataclasses import replace
from typing import Any, Callable

from ..capability_contract import CAPABILITY_ERROR_REASON_KEY
from ..json_utils import json_object
from ..loop import (
    LoopCheckName,
    LoopDecisionKind,
    LoopDecisionSummary,
    LoopReason,
    TraceFailureDomain,
    TraceOutcome,
    TracePhase,
    TraceRunStatus,
    TraceRunType,
    TraceRunView,
    classify_loop_blocked,
    classify_loop_failure,
    loop_decision_summary,
)
from .models import (
    TraceEvent,
    TraceEvaluationDraft,
    TraceEvaluationRule,
    LoopDecisionEvaluationRule,
    LOOP_DECISION_PHASE,
)


def _event_output(event: TraceEvent) -> dict[str, Any]:
    return json_object(event.output_json)


def _event_input(event: TraceEvent) -> dict[str, Any]:
    return json_object(event.input_json)


def _loop_decision_events(events: list[TraceEvent]) -> list[tuple[TraceEvent, dict[str, Any]]]:
    decisions: list[tuple[TraceEvent, dict[str, Any]]] = []
    for event in events:
        if event.phase != LOOP_DECISION_PHASE:
            continue
        output = _event_output(event)
        if output:
            decisions.append((event, output))
    return decisions


def _base_trace_evidence(events: list[TraceEvent]) -> dict[str, Any]:
    evidence: dict[str, Any] = {"event_count": len(events)}
    role_events = [event for event in events if event.phase == TracePhase.AGENT_ROLE_RESULT]
    if role_events:
        evidence["agent_role_results"] = [
            {"model_role": event.model_role, "message": event.message} for event in role_events
        ]
    loop_decisions = _loop_decision_events(events)
    if loop_decisions:
        evidence["loop_decisions"] = [
            _loop_decision_summary(event, output) for event, output in loop_decisions
        ]
    return evidence


def _evaluate_trace_with_rules(
    events: list[TraceEvent],
    evidence: dict[str, Any],
) -> TraceEvaluationDraft:
    for rule in TRACE_EVALUATION_RULES:
        draft = rule(events, evidence)
        if draft is not None:
            return draft
    return _evaluation(
        TraceOutcome.SUCCESS,
        TraceFailureDomain.NONE,
        evidence,
        rule="no_failed_or_degraded_rule",
    )


def _first_failure(events: list[TraceEvent]) -> TraceEvent | None:
    return next((event for event in events if not event.ok), None)


def _record_first_failure_evidence(event: TraceEvent, evidence: dict[str, Any]) -> None:
    evidence["first_failure_phase"] = event.phase
    evidence["first_failure_tool"] = event.tool


_RECOVERY_COMPLETION_REASONS = frozenset(
    {
        str(LoopReason.COMPLETION_EVIDENCE_TRUE),
        str(LoopReason.TERMINAL_RESULT),
        str(LoopReason.WORKFLOW_VERIFIER_PASSED),
    }
)


def _successful_completion_after(
    events: list[TraceEvent], failure: TraceEvent
) -> LoopDecisionSummary | None:
    failure_seen = False
    for event in events:
        if event.id == failure.id:
            failure_seen = True
            continue
        if not failure_seen or event.phase != LOOP_DECISION_PHASE or not event.ok:
            continue
        output = _event_output(event)
        if not output:
            continue
        summary = loop_decision_summary(
            output,
            event_tool=event.tool,
            event_run_id=event.run_id,
        )
        if summary.decision != str(LoopDecisionKind.FINALIZE):
            continue
        if summary.failure_domain not in {"", str(TraceFailureDomain.NONE)}:
            continue
        if summary.failed_checkers or summary.failed_gates:
            continue
        if summary.reason in _RECOVERY_COMPLETION_REASONS:
            return summary
    return None


def _record_recovery_evidence(
    recovery: LoopDecisionSummary, evidence: dict[str, Any]
) -> None:
    evidence["recovered_after_first_failure"] = True
    evidence["recovery_decision"] = recovery.to_dict()


def _evaluation(
    outcome: str,
    failure_domain: str,
    evidence: dict[str, Any],
    *,
    rule: str,
) -> TraceEvaluationDraft:
    evidence["evaluation_rule"] = rule
    return TraceEvaluationDraft(outcome, failure_domain)


def _loop_decision_summary(event: TraceEvent, output: dict[str, Any]) -> dict[str, Any]:
    return loop_decision_summary(
        output,
        event_tool=event.tool,
        event_run_id=event.run_id,
    ).to_dict()


def _loop_decision_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    decisions = _loop_decision_events(events)
    if not decisions:
        return None
    decision_items = tuple(
        (
            loop_decision_summary(
                output,
                event_tool=event.tool,
                event_run_id=event.run_id,
            ),
            output,
        )
        for event, output in decisions
    )

    for classifier in LOOP_DECISION_EVALUATION_RULES:
        for summary, output in reversed(decision_items):
            draft = classifier(summary, output, events, evidence)
            if draft is not None:
                return draft

    return None


def _failed_loop_decision_rule(
    summary: LoopDecisionSummary,
    output: dict[str, Any],
    events: list[TraceEvent],
    evidence: dict[str, Any],
) -> TraceEvaluationDraft | None:
    if summary.decision != LoopDecisionKind.FAILED:
        return None
    failure_domain = classify_loop_failure(output)
    rule = "loop_decision_failed"
    if failure_domain == TraceFailureDomain.CAPABILITY_FAILURE:
        first_failure = _first_failure(events)
        if first_failure is not None and _capability_result_is_input_schema_mismatch(first_failure):
            evidence["failure_domain_corrected_from"] = str(
                TraceFailureDomain.CAPABILITY_FAILURE
            )
            failure_domain = TraceFailureDomain.PLANNER_OR_PARSER
            rule = "loop_failure_domain_corrected_by_input_schema"
    return _evaluation(
        TraceOutcome.FAILURE,
        failure_domain,
        evidence,
        rule=rule,
    )


def _blocked_loop_decision_rule(
    summary: LoopDecisionSummary,
    output: dict[str, Any],
    events: list[TraceEvent],
    evidence: dict[str, Any],
) -> TraceEvaluationDraft | None:
    del events
    if summary.decision == LoopDecisionKind.BLOCKED:
        return _evaluation(
            TraceOutcome.FAILURE,
            classify_loop_blocked(output),
            evidence,
            rule="loop_decision_blocked",
        )
    return None


def _converged_loop_decision_rule(
    summary: LoopDecisionSummary,
    output: dict[str, Any],
    events: list[TraceEvent],
    evidence: dict[str, Any],
) -> TraceEvaluationDraft | None:
    del output, events
    if summary.decision != LoopDecisionKind.CONVERGED:
        return None
    return _evaluation(
        TraceOutcome.DEGRADED,
        TraceFailureDomain.LOOP_NO_PROGRESS,
        evidence,
        rule="loop_decision_converged",
    )


LOOP_DECISION_EVALUATION_RULES: tuple[LoopDecisionEvaluationRule, ...] = (
    _failed_loop_decision_rule,
    _blocked_loop_decision_rule,
    _converged_loop_decision_rule,
)


def _first_failure_rule(
    events: list[TraceEvent],
    evidence: dict[str, Any],
    *,
    phase: str,
    failure_domain: str,
    rule: str,
    predicate: Callable[[TraceEvent], bool] | None = None,
) -> TraceEvaluationDraft | None:
    failure = _first_failure(events)
    if failure is None or failure.phase != phase:
        return None
    if predicate is not None and not predicate(failure):
        return None
    _record_first_failure_evidence(failure, evidence)
    return _evaluation(TraceOutcome.FAILURE, failure_domain, evidence, rule=rule)


def _planner_failure_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    failure = _first_failure(events)
    if failure is None or failure.phase not in {
        TracePhase.PLANNER_SYSCALL,
        TracePhase.PLANNER_PARSE_ERROR,
    }:
        return None
    _record_first_failure_evidence(failure, evidence)
    recovery = _successful_completion_after(events, failure)
    if recovery is not None:
        _record_recovery_evidence(recovery, evidence)
        return _evaluation(
            TraceOutcome.DEGRADED,
            TraceFailureDomain.PLANNER_OR_PARSER,
            evidence,
            rule="planner_failed_then_recovered",
        )
    return _evaluation(
        TraceOutcome.FAILURE,
        TraceFailureDomain.PLANNER_OR_PARSER,
        evidence,
        rule="planner_failed_event",
    )


def _safeguard_failure_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    return _first_failure_rule(
        events,
        evidence,
        phase=TracePhase.CAPABILITY_RESULT,
        failure_domain=TraceFailureDomain.SAFEGUARD_POLICY,
        rule="safeguard_hook_decision",
        predicate=_capability_result_has_safeguard_decision,
    )


def _capability_failure_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    failure = _first_failure(events)
    if failure is None or failure.phase != TracePhase.CAPABILITY_RESULT:
        return None
    _record_first_failure_evidence(failure, evidence)
    if _capability_result_is_input_schema_mismatch(failure):
        recovery = _successful_completion_after(events, failure)
        if recovery is not None:
            _record_recovery_evidence(recovery, evidence)
            return _evaluation(
                TraceOutcome.DEGRADED,
                TraceFailureDomain.PLANNER_OR_PARSER,
                evidence,
                rule="capability_input_schema_mismatch_then_recovered",
            )
        return _evaluation(
            TraceOutcome.FAILURE,
            TraceFailureDomain.PLANNER_OR_PARSER,
            evidence,
            rule="capability_input_schema_mismatch",
        )
    recovery = _successful_completion_after(events, failure)
    if recovery is not None:
        _record_recovery_evidence(recovery, evidence)
        return _evaluation(
            TraceOutcome.DEGRADED,
            TraceFailureDomain.CAPABILITY_FAILURE,
            evidence,
            rule="capability_failed_then_recovered",
        )
    return _evaluation(
        TraceOutcome.FAILURE,
        TraceFailureDomain.CAPABILITY_FAILURE,
        evidence,
        rule="capability_failed_event",
    )


def _checker_failure_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    failure = _first_failure(events)
    if failure is None or failure.phase != LoopPhase.CHECK:
        return None
    _record_first_failure_evidence(failure, evidence)
    recovery_plan = next((event for event in events if event.phase == LoopPhase.RECOVERY), None)
    if recovery_plan:
        evidence["recovery_plan_recorded"] = True
        recovery_output = _event_output(recovery_plan)
        evidence["recovery_blocked"] = bool(recovery_output.get("blocked", True))
        details = recovery_output.get("details")
        if isinstance(details, dict):
            evidence["recovery_detail_keys"] = sorted(details)
        rule = "checker_failed_after_recovery_plan"
    else:
        evidence["recovery_plan_recorded"] = False
        rule = "checker_failed_without_recovery_plan"
    return _evaluation(TraceOutcome.FAILURE, TraceFailureDomain.CHECKER_BLOCKED, evidence, rule=rule)


def _runtime_failure_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    failure = _first_failure(events)
    if failure is None:
        return None
    _record_first_failure_evidence(failure, evidence)
    return _evaluation(
        TraceOutcome.FAILURE,
        TraceFailureDomain.RUNTIME,
        evidence,
        rule="runtime_failed_event",
    )


def _planner_no_response_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    if not _planner_call_started_without_result(events):
        return None
    evidence["planner_call_without_result"] = True
    return _evaluation(
        TraceOutcome.FAILURE,
        TraceFailureDomain.PROVIDER_NO_RESPONSE,
        evidence,
        rule="planner_call_without_result",
    )


def _missing_trace_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    if events:
        return None
    return _evaluation(
        TraceOutcome.UNKNOWN,
        TraceFailureDomain.TRACE_MISSING,
        evidence,
        rule="trace_missing",
    )


TRACE_EVALUATION_RULES: tuple[TraceEvaluationRule, ...] = (
    _loop_decision_rule,
    _planner_failure_rule,
    _safeguard_failure_rule,
    _capability_failure_rule,
    _checker_failure_rule,
    _runtime_failure_rule,
    _planner_no_response_rule,
    _missing_trace_rule,
)


def _capability_result_has_safeguard_decision(event: TraceEvent) -> bool:
    output = _event_output(event)
    facts = output.get("facts")
    return isinstance(facts, dict) and isinstance(facts.get("hook_decision"), dict)


def _capability_result_is_approval_request(facts: Any) -> bool:
    return (
        isinstance(facts, dict)
        and str(facts.get("entity_type") or "") == "approval_request"
        and isinstance(facts.get("approval"), dict)
    )


def _capability_result_is_input_schema_mismatch(event: TraceEvent) -> bool:
    output = _event_output(event)
    facts = output.get("facts")
    if not isinstance(facts, dict):
        return False
    if facts.get(CAPABILITY_ERROR_REASON_KEY) != "schema_mismatch":
        return False
    return "result_action" not in facts


def _has_approval_required_pause(events: list[TraceEvent]) -> bool:
    for event, output in reversed(_loop_decision_events(events)):
        summary = loop_decision_summary(
            output,
            event_tool=event.tool,
            event_run_id=event.run_id,
        )
        if summary.decision != str(LoopDecisionKind.PAUSE_FOR_APPROVAL):
            continue
        if summary.reason != str(LoopReason.APPROVAL_REQUIRED):
            continue
        if summary.failure_domain not in {"", str(TraceFailureDomain.NONE)}:
            continue
        if _loop_results_include(
            output.get("gate_results"),
            name=str(LoopCheckName.APPROVAL_GATE),
            passed=True,
        ):
            return True
    return False


def _loop_results_include(value: Any, *, name: str, passed: bool) -> bool:
    if not isinstance(value, list):
        return False
    return any(
        isinstance(item, dict)
        and str(item.get("name") or "") == name
        and item.get("passed") is passed
        for item in value
    )


def _planner_call_started_without_result(events: list[TraceEvent]) -> bool:
    phases = [event.phase for event in events]
    return TracePhase.PLANNER_CALL_START in phases and not any(
        phase in {TracePhase.PLANNER_SYSCALL, TracePhase.PLANNER_CALL_ERROR, TracePhase.TURN_FINAL}
        for phase in phases
    )
