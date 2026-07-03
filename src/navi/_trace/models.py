from __future__ import annotations
from navi.lifecycle import Phase, Governance, Acceptance, Resolution

from dataclasses import dataclass
from typing import Any, Callable

from ..json_utils import json_object
from ..loop import LoopDecisionSummary, LoopPhase

LOOP_DECISION_PHASE = LoopPhase.DECISION

@dataclass(frozen=True)
class TraceEvent:
    id: str
    trace_id: str
    session_id: str
    run_id: str
    phase: str
    source: str
    peer_id: str
    sender_id: str
    tool: str
    model_role: str
    ok: bool
    input_json: str
    output_json: str
    message: str
    created_at: float


@dataclass(frozen=True)
class TraceEvaluation:
    id: str
    trace_id: str
    outcome: str
    failure_domain: str
    evidence_json: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "trace_id": self.trace_id,
            "outcome": self.outcome,
            "failure_domain": self.failure_domain,
            "evidence": json_object(self.evidence_json),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class TraceEvaluationDraft:
    outcome: str
    failure_domain: str


TraceEvaluationRule = Callable[[list[TraceEvent], dict[str, Any]], TraceEvaluationDraft | None]
LoopDecisionEvaluationRule = Callable[
    [LoopDecisionSummary, dict[str, Any], list[TraceEvent], dict[str, Any]],
    TraceEvaluationDraft | None,
]
