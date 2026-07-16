"""Approval decision constants for workflow approval flows."""

from __future__ import annotations

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
