from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MUTATING_TOOLS = frozenset(
    {
        "file.write",
        "shell.run",
        "test.run",
        "approval.resolve",
        "watch.create",
        "watch.delete",
        "delegate.delete",
    }
)


@dataclass(frozen=True)
class CriticDecision:
    passed: bool
    findings: list[str]
    recommendation: str
    evidence: dict[str, Any]


def review_execution_protocol(protocol: Any) -> CriticDecision:
    """Deterministic completion gate for actuator-backed execution results."""

    evidence = protocol.evidence if isinstance(protocol.evidence, list) else []
    verification = protocol.verification if isinstance(protocol.verification, dict) else {}
    completion = protocol.completion if isinstance(protocol.completion, dict) else {}
    actions = protocol.actions if isinstance(protocol.actions, list) else []

    findings: list[str] = []
    if completion.get("status") != "completed":
        findings.append(f"completion status is {completion.get('status') or 'missing'}")
    if verification.get("status") != "verified":
        findings.append(f"verification status is {verification.get('status') or 'missing'}")
    if not evidence:
        findings.append("execution protocol evidence is empty")

    capability_results = [
        item
        for item in evidence
        if isinstance(item, dict) and item.get("kind") == "capability_result"
    ]
    verification_results = [
        item
        for item in evidence
        if isinstance(item, dict) and item.get("kind") == "verification_result"
    ]
    failed_capabilities = [item for item in capability_results if not item.get("ok")]
    failed_checks = [item for item in verification_results if not item.get("ok")]
    if not capability_results:
        findings.append("no capability result evidence recorded")
    if failed_capabilities:
        first = failed_capabilities[0]
        findings.append(f"capability failed: {first.get('tool') or 'unknown'}")
    if failed_checks:
        first = failed_checks[0]
        findings.append(f"verification check failed: {first.get('check') or 'unknown'}")

    mutating_actions = [
        action
        for action in actions
        if isinstance(action, dict)
        and (
            str(action.get("permission") or "").lower() == "write"
            or str(action.get("tool") or "") in MUTATING_TOOLS
        )
    ]
    if mutating_actions and not verification_results:
        findings.append("mutating execution lacks independent verification checks")

    passed = not findings
    recommendation = (
        "complete" if passed else "block completion and repair or retry with stronger verification"
    )
    return CriticDecision(
        passed=passed,
        findings=findings,
        recommendation=recommendation,
        evidence={
            "critic": "deterministic_execution_gate",
            "passed": passed,
            "finding_count": len(findings),
            "capability_result_count": len(capability_results),
            "verification_result_count": len(verification_results),
            "mutating_action_count": len(mutating_actions),
            "recommendation": recommendation,
        },
    )
