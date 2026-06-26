from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RunStatus(StrEnum):
    PENDING = "pending"
    PREPARING = "preparing"
    PREPARED = "prepared"
    AWAITING_APPROVAL = "awaiting_approval"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    REJECTED = "rejected"


# Backwards-compatible string aliases.
RUN_STATUS_PENDING = RunStatus.PENDING
RUN_STATUS_PREPARING = RunStatus.PREPARING
RUN_STATUS_PREPARED = RunStatus.PREPARED
RUN_STATUS_AWAITING_APPROVAL = RunStatus.AWAITING_APPROVAL
RUN_STATUS_QUEUED = RunStatus.QUEUED
RUN_STATUS_RUNNING = RunStatus.RUNNING
RUN_STATUS_COMPLETED = RunStatus.COMPLETED
RUN_STATUS_FAILED = RunStatus.FAILED
RUN_STATUS_BLOCKED = RunStatus.BLOCKED
RUN_STATUS_REJECTED = RunStatus.REJECTED

RUN_TERMINAL_STATUSES = frozenset(
    {
        RUN_STATUS_COMPLETED,
        RUN_STATUS_FAILED,
        RUN_STATUS_BLOCKED,
        RUN_STATUS_REJECTED,
    }
)


@dataclass(frozen=True)
class RunFinalizeDecision:
    status: str
    error: str


@dataclass(frozen=True)
class AcceptanceAdvance:
    action: str
    terminal: bool = False
    error: str = ""


def run_is_terminal(status: str) -> bool:
    return status in RUN_TERMINAL_STATUSES


def prepare_run_status(*, exit_code: int, current_status: str = "") -> str:
    if current_status == RUN_STATUS_AWAITING_APPROVAL:
        return RUN_STATUS_AWAITING_APPROVAL
    return RUN_STATUS_PREPARED if exit_code == 0 else RUN_STATUS_FAILED


def execute_finalize_decision(
    *,
    exit_code: int,
    stderr: str,
    completion_status: str = "",
) -> RunFinalizeDecision:
    if completion_status == RUN_STATUS_BLOCKED:
        error = stderr.strip() if stderr else "execution blocked"
        return RunFinalizeDecision(status=RUN_STATUS_BLOCKED, error=error)
    if completion_status == RUN_STATUS_AWAITING_APPROVAL:
        return RunFinalizeDecision(status=RUN_STATUS_AWAITING_APPROVAL, error="")
    if exit_code == 0:
        return RunFinalizeDecision(status=RUN_STATUS_COMPLETED, error="")
    error = stderr.strip() if stderr else "actuator loop failed"
    return RunFinalizeDecision(status=RUN_STATUS_FAILED, error=error)


def execution_ledger_reason(exit_code: int) -> str:
    outcome = RUN_STATUS_COMPLETED if exit_code == 0 else RUN_STATUS_FAILED
    return f"run execution {outcome}"


ACCEPTANCE_ADVANCE_BY_STATUS: dict[str, AcceptanceAdvance] = {
    RUN_STATUS_AWAITING_APPROVAL: AcceptanceAdvance(action="approve"),
    RUN_STATUS_QUEUED: AcceptanceAdvance(action="process_queue"),
    RUN_STATUS_COMPLETED: AcceptanceAdvance(action="terminal", terminal=True),
    RUN_STATUS_FAILED: AcceptanceAdvance(action="terminal", terminal=True),
    RUN_STATUS_BLOCKED: AcceptanceAdvance(action="terminal", terminal=True),
    RUN_STATUS_REJECTED: AcceptanceAdvance(action="terminal", terminal=True),
}


def acceptance_advance(status: str) -> AcceptanceAdvance:
    return ACCEPTANCE_ADVANCE_BY_STATUS.get(
        status,
        AcceptanceAdvance(action="stalled", error=f"run cannot advance from status {status}"),
    )


def acceptance_outcome(*, accepted: bool, run_status: str) -> str:
    if accepted:
        return "accepted"
    if run_status in {
        RUN_STATUS_BLOCKED,
        RUN_STATUS_AWAITING_APPROVAL,
        RUN_STATUS_QUEUED,
        RUN_STATUS_RUNNING,
    }:
        return "blocked"
    return "failed"
