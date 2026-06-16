from __future__ import annotations

from dataclasses import dataclass


RUN_STATUS_PENDING = "pending"
RUN_STATUS_PREPARING = "preparing"
RUN_STATUS_PREPARED = "prepared"
RUN_STATUS_AWAITING_APPROVAL = "awaiting_approval"
RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_BLOCKED = "blocked"
RUN_STATUS_REJECTED = "rejected"

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
) -> RunFinalizeDecision:
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
