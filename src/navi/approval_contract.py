"""Approval decision constants for workflow approval flows."""

from __future__ import annotations

from typing import Any

APPROVAL_DECISION_APPROVE = "approve"
APPROVAL_DECISION_REJECT = "reject"
APPROVAL_DECISIONS = frozenset({APPROVAL_DECISION_APPROVE, APPROVAL_DECISION_REJECT})

APPROVAL_STATUS_PENDING = "pending"
APPROVAL_STATUS_APPROVED = "approved"
APPROVAL_STATUS_REJECTED = "rejected"
APPROVAL_STATUS_EXPIRED = "expired"

APPROVAL_ACTION_RUN_EXECUTION = "run_execution"
APPROVAL_ACTION_SESSION_ELEVATION = "session_elevation"
APPROVAL_ACTION_CAPABILITY = "capability"
APPROVAL_ACTION_EVOLUTION = "evolution"


def owned_approval_gate_id(evidence: dict[str, Any]) -> str:
    """Return the approval entity directly owned by one waiting LoopRun.

    Continuation facts may mention a pending approval owned by another LoopRun.
    Only the capability result's own approval request establishes that the
    current LoopRun may remain at an approval gate.
    """
    for container in (
        evidence,
        evidence.get("capability_result") if isinstance(evidence, dict) else None,
        evidence.get("executor") if isinstance(evidence, dict) else None,
    ):
        if not isinstance(container, dict):
            continue
        facts = container.get("facts")
        if not isinstance(facts, dict):
            continue
        approval = facts.get("approval")
        if isinstance(approval, dict) and str(approval.get("id") or ""):
            return str(approval["id"])
        entity_id = str(facts.get("entity_id") or "")
        if facts.get("entity_type") == "approval_request" and entity_id:
            return entity_id
    return ""
