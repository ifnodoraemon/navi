from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from ..evolution import EvolutionEngine, EvolutionLedger
from ..tools import ToolSpec
from .helpers import arg_text as _arg_text
from .helpers import fact_result as _fact_result
from .helpers import transition_facts as _transition_facts


@capability("evolution_propose")
class EvolutionProposeCapability(BaseCapability):

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        try:
            proposal = EvolutionLedger(self.home).propose(
                target_type=_arg_text(args, "target_type"),
                target_id=_arg_text(args, "target_id"),
                reason=_arg_text(args, "reason"),
                expected_benefit=_arg_text(args, "expected_benefit"),
                risk=_arg_text(args, "risk"),
                before=_arg_text(args, "before"),
                after=_arg_text(args, "after"),
                rollback_plan=_arg_text(args, "rollback_plan"),
                required_approval_level=_arg_text(args, "required_approval_level") or "L2",
                evidence=_arg_text(args, "evidence"),
                source_run_id=_arg_text(args, "source_run_id"),
                eval_cases=_string_list(args.get("eval_cases")),
            )
        except ValueError as exc:
            return _evolution_error(str(exc), reason="schema_mismatch")
        proposal_facts = asdict(proposal)
        facts = {
            **_transition_facts("evolution_proposal", proposal.id, "created"),
            "proposal_id": proposal.id,
            "proposal": proposal_facts,
        }
        return _fact_result("evolution", facts, run_id=proposal.id)


@capability("evolution_record_evaluation")
class EvolutionRecordEvaluationCapability(BaseCapability):

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        proposal_id = _arg_text(args, "proposal_id")
        evaluation_result = _arg_text(args, "evaluation_result")
        if not proposal_id or not evaluation_result:
            return _evolution_error(
                "evolution.record_evaluation requires proposal_id and evaluation_result.",
                reason="schema_mismatch",
            )
        try:
            proposal = EvolutionLedger(self.home).record_proposal_evaluation(
                proposal_id,
                evaluation_result,
                approver_id=context.sender_id or context.peer_id,
                approved_at=__import__("time").time(),
            )
        except ValueError as exc:
            return _evolution_error(str(exc), reason="schema_mismatch", proposal_id=proposal_id)
        if proposal is None:
            return _evolution_error(
                "proposal not found", reason="not_found", proposal_id=proposal_id
            )
        proposal_facts = asdict(proposal)
        facts = {
            **_transition_facts("evolution_proposal", proposal.id, "updated"),
            "proposal_id": proposal.id,
            "proposal": proposal_facts,
        }
        return _fact_result("evolution", facts, run_id=proposal.id)


@capability("evolution_apply")
class EvolutionApplyCapability(BaseCapability):

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        proposal_id = _arg_text(args, "proposal_id")
        if not proposal_id:
            return _evolution_error(
                "evolution.apply requires proposal_id.", reason="schema_mismatch"
            )
        try:
            event = EvolutionEngine(self.home).apply_proposal(proposal_id)
        except ValueError as exc:
            return _evolution_error(str(exc), reason="schema_mismatch", proposal_id=proposal_id)
        if event is None:
            return _evolution_error(
                "proposal not found", reason="not_found", proposal_id=proposal_id
            )
        event_facts = asdict(event)
        facts = {
            **_transition_facts("evolution_event", event.id, "created"),
            "proposal_id": proposal_id,
            "event_id": event.id,
            "event": event_facts,
        }
        return _fact_result("evolution", facts, run_id=event.id)


@capability("evolution_rollback")
class EvolutionRollbackCapability(BaseCapability):

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        event_id = _arg_text(args, "event_id")
        if not event_id:
            return _evolution_error(
                "evolution.rollback requires event_id.", reason="schema_mismatch"
            )
        try:
            event = EvolutionEngine(self.home).rollback(event_id)
        except ValueError as exc:
            return _evolution_error(str(exc), reason="schema_mismatch", event_id=event_id)
        if event is None:
            return _evolution_error("event not found", reason="not_found", event_id=event_id)
        event_facts = asdict(event)
        facts = {
            **_transition_facts("evolution_event", event.id, "updated"),
            "event_id": event.id,
            "event": event_facts,
        }
        return _fact_result("evolution", facts, run_id=event.id)


def _evolution_error(
    message: str,
    *,
    reason: str,
    proposal_id: str = "",
    event_id: str = "",
) -> CapabilityResult:
    return CapabilityResult(
        ok=False,
        action="evolution",
        observation=message,
        message=message,
        terminal=False,
        error_reason=reason,
        facts={
            "reason": reason,
            "proposal_id": proposal_id,
            "event_id": event_id,
        },
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]
