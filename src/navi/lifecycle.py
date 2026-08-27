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
