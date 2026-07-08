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
        blocked = any(result.reason == "evidence_missing" for result in failed_required)
        accepted = not failed_required
        if accepted:
            state_hint: LoopTerminalState | str = LoopTerminalState.CONVERGED
        elif timed_out:
            state_hint = LoopTerminalState.TIMED_OUT
        elif blocked:
            state_hint = LoopTerminalState.BLOCKED
        else:
            state_hint = ""
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
        return LoopCheckResult(
            name=step.name,
            passed=not step.required,
            severity=LoopSeverity.ERROR if step.required else LoopSeverity.INFO,
            reason="evidence_missing" if step.required else "optional_evidence_missing",
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
    reason = "exit_code_zero" if passed else "command_timed_out" if timed_out else "exit_code_nonzero"
    checker_fact = _checker_fact(facts)
    return LoopCheckResult(
        name=step.name,
        passed=passed,
        severity=LoopSeverity.INFO if passed else LoopSeverity.ERROR,
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
    if "passed" in facts:
        passed = bool(facts.get("passed"))
    else:
        passed = bool(facts.get("ok"))
    return LoopCheckResult(
        name=step.name,
        passed=passed,
        severity=LoopSeverity.INFO if passed else LoopSeverity.ERROR,
        reason="fact_passed" if passed else "fact_failed",
        evidence={"evidence_key": key, "kind": str(step.kind)},
    )


def _step_is_command_like(step: VerificationStep) -> bool:
    return step.kind in {
        VerificationKind.UNIT_TEST,
        VerificationKind.INTEGRATION_TEST,
        VerificationKind.COMMAND_EXIT_CODE,
    }


def _checker_fact(facts: dict[str, Any]) -> dict[str, Any]:
    value = facts.get("checker_fact")
    return value if isinstance(value, dict) else {}
