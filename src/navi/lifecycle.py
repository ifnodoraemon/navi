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


def run_is_terminal(phase: str) -> bool:
    return phase == Phase.ENDED


def prepare_run_phase(*, exit_code: int) -> str:
    return Phase.RUNNING if exit_code == 0 else Phase.ENDED


def execute_finalize_decision(
    *,
    exit_code: int,
    stderr: str,
) -> RunFinalizeDecision:
    if exit_code == 0:
        return RunFinalizeDecision(phase=Phase.ENDED, resolution=Resolution.SUCCESS, error="")
    error = stderr.strip() if stderr else "actuator loop failed"
    return RunFinalizeDecision(phase=Phase.ENDED, resolution=Resolution.FAILED, error=error)


def execution_ledger_reason(exit_code: int) -> str:
    outcome = Resolution.SUCCESS if exit_code == 0 else Resolution.FAILED
    return f"run execution {outcome}"


def acceptance_advance(
    *,
    phase: str,
    governance: str = Governance.NONE,
    resolution: str = Resolution.NONE,
) -> AcceptanceAdvance:
    if phase == Phase.ENDED:
        return AcceptanceAdvance(action="terminal", terminal=True)
    if governance == Governance.AWAITING_APPROVAL:
        return AcceptanceAdvance(action="approve")
    if phase == Phase.PENDING:
        return AcceptanceAdvance(action="process_queue")
    if phase == Phase.RUNNING:
        return AcceptanceAdvance(action="check")
    return AcceptanceAdvance(
        action="stalled",
        error=f"run cannot advance from phase={phase} governance={governance} resolution={resolution}",
    )


def acceptance_outcome(*, accepted: bool, run_phase: str) -> str:
    if accepted:
        return "accepted"
    if run_phase == Phase.RUNNING:
        return "blocked"
    return "failed"
