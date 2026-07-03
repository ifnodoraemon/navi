from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

class Phase(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    ENDED = "ended"

class Governance(StrEnum):
    NONE = "none"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"

class Acceptance(StrEnum):
    NONE = "none"
    UNVERIFIED = "unverified"
    ACCEPTED = "accepted"
    REJECTED = "rejected"

class Resolution(StrEnum):
    NONE = "none"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELED = "canceled"
    BLOCKED = "blocked"

def is_terminal_phase(phase: str) -> bool:
    return phase == Phase.ENDED

class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"


RUN_STATUS_PENDING = RunStatus.PENDING
RUN_STATUS_RUNNING = RunStatus.RUNNING
RUN_STATUS_AWAITING_APPROVAL = RunStatus.AWAITING_APPROVAL
RUN_STATUS_COMPLETED = RunStatus.COMPLETED
RUN_STATUS_FAILED = RunStatus.FAILED



@dataclass(frozen=True)
class RunFinalizeDecision:
    phase: str
    resolution: str
    error: str


@dataclass(frozen=True)
class AcceptanceAdvance:
    action: str
    terminal: bool = False
    error: str = ""


def run_is_terminal(status: str) -> bool:
    return status in RUN_TERMINAL_STATUSES


def prepare_run_status(*, exit_code: int, current_status: str = "") -> str:
    return "running" if exit_code == 0 else "failed"


def execute_finalize_decision(
    *,
    exit_code: int,
    stderr: str,
    completion_status: str = "",
) -> RunFinalizeDecision:
    if exit_code == 0:
        return RunFinalizeDecision(phase=Phase.ENDED, resolution=Resolution.SUCCESS, error="")
    error = stderr.strip() if stderr else "actuator loop failed"
    return RunFinalizeDecision(phase=Phase.ENDED, resolution=Resolution.FAILED, error=error)


def execution_ledger_reason(exit_code: int) -> str:
    outcome = RUN_STATUS_COMPLETED if exit_code == 0 else RUN_STATUS_FAILED
    return f"run execution {outcome}"


ACCEPTANCE_ADVANCE_BY_STATUS: dict[str, AcceptanceAdvance] = {
    RUN_STATUS_PENDING: AcceptanceAdvance(action="confirm"),
    RUN_STATUS_RUNNING: AcceptanceAdvance(action="check"),
    RUN_STATUS_AWAITING_APPROVAL: AcceptanceAdvance(action="approve"),
    RUN_STATUS_COMPLETED: AcceptanceAdvance(action="terminal", terminal=True),
    RUN_STATUS_FAILED: AcceptanceAdvance(action="terminal", terminal=True),
}


def acceptance_advance(status: str) -> AcceptanceAdvance:
    return ACCEPTANCE_ADVANCE_BY_STATUS.get(
        status,
        AcceptanceAdvance(action="stalled", error=f"run cannot advance from status {status}"),
    )


def acceptance_outcome(*, accepted: bool, run_status: str) -> str:
    if accepted:
        return "accepted"
    if run_status == RUN_STATUS_RUNNING:
        return "blocked"
    return "failed"
