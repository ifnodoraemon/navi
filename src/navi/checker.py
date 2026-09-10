from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .loop import LoopCheckResult, LoopSeverity
from .loop_contracts import LoopSpec, LoopTerminalState, VerificationKind, VerificationStep


@dataclass(frozen=True)
class CheckerReport:
    accepted: bool
    blocked: bool
    timed_out: bool
    state_hint: LoopTerminalState | str = ""
    checker_results: tuple[LoopCheckResult, ...] = ()
    evidence: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "blocked": self.blocked,
            "timed_out": self.timed_out,
            "state_hint": str(self.state_hint),
            "checker_results": [item.to_dict() for item in self.checker_results],
            "evidence": dict(self.evidence or {}),
        }


class DeterministicChecker:
    """Evaluate a LoopSpec verification ladder from objective facts only."""

    def evaluate(self, spec: LoopSpec, evidence: dict[str, Any]) -> CheckerReport:
        spec.validate()
        results = tuple(_evaluate_step(step, evidence) for step in spec.verification_ladder)
        failed_required = [
            result
            for step, result in zip(spec.verification_ladder, results, strict=True)
            if step.required and not result.passed
        ]
        timed_out = any(
            result.evidence.get("timed_out") is True
            or result.evidence.get("error_type") == "TimeoutError"
            for result in failed_required
        )
        blocked = any(
            result.reason in {"evidence_missing", "no_route_available"}
            for result in failed_required
        )
        accepted = not failed_required
        state_hints: dict[tuple[bool, bool, bool], LoopTerminalState | str] = {
            (True, False, False): LoopTerminalState.CONVERGED,
            (True, True, False): LoopTerminalState.CONVERGED,
            (True, False, True): LoopTerminalState.CONVERGED,
            (True, True, True): LoopTerminalState.CONVERGED,
            (False, True, False): LoopTerminalState.TIMED_OUT,
            (False, True, True): LoopTerminalState.TIMED_OUT,
            (False, False, True): LoopTerminalState.BLOCKED,
            (False, False, False): "",
        }
        state_hint = state_hints[(accepted, timed_out, blocked)]
        return CheckerReport(
            accepted=accepted,
            blocked=blocked,
            timed_out=timed_out,
            state_hint=state_hint,
            checker_results=results,
            evidence={"required_failed": len(failed_required)},
        )


def _evaluate_step(step: VerificationStep, evidence: dict[str, Any]) -> LoopCheckResult:
    key = step.evidence_key or step.name
    facts = evidence.get(key)
    if facts is None:
        severity_map = {True: LoopSeverity.ERROR, False: LoopSeverity.INFO}
        reason_map = {True: "evidence_missing", False: "optional_evidence_missing"}
        return LoopCheckResult(
            name=step.name,
            passed=not step.required,
            severity=severity_map[step.required],
            reason=reason_map[step.required],
            evidence={"evidence_key": key},
        )
    if not isinstance(facts, dict):
        return LoopCheckResult(
            name=step.name,
            passed=False,
            severity=LoopSeverity.ERROR,
            reason="evidence_not_object",
            evidence={"evidence_key": key, "type": type(facts).__name__},
        )
    if _step_is_command_like(step):
        return _evaluate_command_step(step, key=key, facts=facts)
    return _evaluate_boolean_step(step, key=key, facts=facts)


def _evaluate_command_step(step: VerificationStep, *, key: str, facts: dict[str, Any]) -> LoopCheckResult:
    timed_out = bool(facts.get("timed_out")) or _checker_fact(facts).get("error_type") == "TimeoutError"
    exit_code = facts.get("exit_code")
    passed = exit_code == 0 and not timed_out
    reason_map = {
        (True, False): "exit_code_zero",
        (True, True): "command_timed_out",
        (False, True): "command_timed_out",
        (False, False): "exit_code_nonzero",
    }
    reason = reason_map[(passed, timed_out)]
    checker_fact = _checker_fact(facts)
    severity_map = {True: LoopSeverity.INFO, False: LoopSeverity.ERROR}
    return LoopCheckResult(
        name=step.name,
        passed=passed,
        severity=severity_map[passed],
        reason=reason,
        evidence={
            "evidence_key": key,
            "kind": str(step.kind),
            "exit_code": exit_code,
            "timed_out": timed_out,
            "error_type": checker_fact.get("error_type", ""),
        },
    )


def _evaluate_boolean_step(step: VerificationStep, *, key: str, facts: dict[str, Any]) -> LoopCheckResult:
    target_key = {True: "passed", False: "ok"}["passed" in facts]
    passed = bool(facts.get(target_key))
    is_llm = step.kind == VerificationKind.LLM_CHECKER
    reason_map = {
        (True, True): "fact_passed",
        (True, False): "fact_passed",
        (False, True): "semantic_check_failed",
        (False, False): "fact_failed",
    }
    reason = reason_map[(passed, is_llm)]
    severity_map = {True: LoopSeverity.INFO, False: LoopSeverity.ERROR}
    return LoopCheckResult(
        name=step.name,
        passed=passed,
        severity=severity_map[passed],
        reason=reason,
        evidence={
            "evidence_key": key,
            "kind": str(step.kind),
            "evidence_summary": str(facts.get("evidence_summary") or ""),
            "evaluator_role": str(facts.get("evaluator_role") or ""),
            "isolated_context": bool(facts.get("isolated_context", False)),
        },
    )


def _step_is_command_like(step: VerificationStep) -> bool:
    return step.kind in {
        VerificationKind.UNIT_TEST,
        VerificationKind.INTEGRATION_TEST,
        VerificationKind.COMMAND_EXIT_CODE,
    }


def _checker_fact(facts: dict[str, Any]) -> dict[str, Any]:
    value = facts.get("checker_fact")
    if isinstance(value, dict):
        return value
    return {}
