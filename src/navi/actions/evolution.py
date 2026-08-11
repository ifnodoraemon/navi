from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from ..evolution import EvolutionLedger
from ..evolution_candidates import EvolutionCandidateScanner
from ..evolution_engine import EvolutionEngine
from ..evolution_experiments import EvolutionExperimentStore
from ..evolution_targets import EvolutionTargetAdapterRegistry
from .helpers import arg_text as _arg_text
from .helpers import failure_result as _failure_result
from .helpers import fact_result as _fact_result
from .helpers import transition_facts as _transition_facts


@capability("evolution_candidates")
class EvolutionCandidatesCapability(BaseCapability):
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        del permission, context
        candidates = EvolutionCandidateScanner(self.home).scan(
            window_days=_bounded_int(args.get("window_days"), default=7, lower=1, upper=90),
            min_occurrences=_bounded_int(
                args.get("min_occurrences"), default=3, lower=2, upper=100
            ),
            limit=_bounded_int(args.get("limit"), default=100, lower=1, upper=500),
        )
        facts = {
            **_transition_facts("evolution_candidates", "recent-trace-failures", "observed"),
            "candidates": [candidate.to_dict() for candidate in candidates],
            "count": len(candidates),
            "semantic_decision": "model_review_required",
            "automatic_proposal_created": False,
            "automatic_apply_allowed": False,
        }
        return _fact_result("evolution", facts)


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
            target_type = _arg_text(args, "target_type")
            target_id = _arg_text(args, "target_id")
            candidate_value = args.get("after")
            candidate = candidate_value if isinstance(candidate_value, str) else ""
            targets = EvolutionTargetAdapterRegistry(self.home)
            adapter = targets.get(target_type)
            before = adapter.read(target_id)
            validation = adapter.validate(target_id, candidate)
            proposal = EvolutionLedger(self.home).propose(
                target_type=target_type,
                target_id=target_id,
                reason=_arg_text(args, "reason"),
                expected_benefit=_arg_text(args, "expected_benefit"),
                risk=_arg_text(args, "risk"),
                before=before,
                after=candidate,
                rollback_plan=_arg_text(args, "rollback_plan"),
                required_approval_level=_arg_text(args, "required_approval_level") or "L2",
                evidence=_arg_text(args, "evidence"),
                source_run_id=(
                    context.loop_run_id
                    or context.trace_id
                    or _arg_text(args, "source_run_id")
                ),
                eval_cases=_string_list(args.get("eval_cases")),
            )
        except (OSError, ValueError) as exc:
            return _evolution_error(str(exc), reason="schema_mismatch")
        proposal_facts = _proposal_facts(proposal)
        facts = {
            **_transition_facts("evolution_proposal", proposal.id, "created"),
            "proposal_id": proposal.id,
            "proposal": proposal_facts,
            "target_validation": validation,
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
                evaluation_evidence=_arg_text(args, "evaluation_evidence"),
                approval_id=_arg_text(args, "approval_id"),
            )
        except ValueError as exc:
            return _evolution_error(str(exc), reason="schema_mismatch", proposal_id=proposal_id)
        if proposal is None:
            return _evolution_error(
                "proposal not found", reason="not_found", proposal_id=proposal_id
            )
        proposal_facts = _proposal_facts(proposal)
        facts = {
            **_transition_facts("evolution_proposal", proposal.id, "updated"),
            "proposal_id": proposal.id,
            "proposal": proposal_facts,
        }
        return _fact_result("evolution", facts, run_id=proposal.id)


@capability("evolution_experiment")
class EvolutionExperimentCapability(BaseCapability):
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
                "evolution.experiment requires proposal_id.", reason="schema_mismatch"
            )
        try:
            experiment = EvolutionExperimentStore(self.home).run(proposal_id)
        except KeyError as exc:
            return _evolution_error(str(exc), reason="not_found", proposal_id=proposal_id)
        except ValueError as exc:
            return _evolution_error(
                str(exc), reason="schema_mismatch", proposal_id=proposal_id
            )
        facts = {
            **_transition_facts("evolution_experiment", experiment.id, experiment.status),
            "proposal_id": proposal_id,
            "experiment": experiment.to_dict(),
        }
        return _fact_result("evolution", facts, run_id=experiment.id)


@capability("evolution_observe")
class EvolutionObserveCapability(BaseCapability):
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
                "evolution.observe requires event_id.", reason="schema_mismatch"
            )
        try:
            activation = EvolutionExperimentStore(self.home).observe(
                event_id,
                successes=_nonnegative_int(args.get("successes")),
                errors=_nonnegative_int(args.get("errors")),
                evidence=args.get("evidence") if isinstance(args.get("evidence"), dict) else {},
                rollback=EvolutionEngine(self.home).rollback,
            )
        except KeyError as exc:
            return _evolution_error(str(exc), reason="not_found", event_id=event_id)
        except ValueError as exc:
            return _evolution_error(str(exc), reason="schema_mismatch", event_id=event_id)
        facts = {
            **_transition_facts("evolution_activation", activation.id, activation.status),
            "event_id": event_id,
            "activation": activation.to_dict(),
        }
        return _fact_result("evolution", facts, run_id=activation.id)


@capability("evolution_state")
class EvolutionStateCapability(BaseCapability):
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        proposal_id = _arg_text(args, "proposal_id")
        event_id = _arg_text(args, "event_id")
        ledger = EvolutionLedger(self.home)
        experiments = EvolutionExperimentStore(self.home)
        targets = EvolutionTargetAdapterRegistry(self.home)
        proposal = ledger.get_proposal(proposal_id) if proposal_id else None
        experiment = experiments.latest_experiment(proposal_id) if proposal_id else None
        activation = experiments.activation_for_event(event_id) if event_id else None
        if proposal_id and proposal is None:
            return _evolution_error(
                "proposal not found", reason="not_found", proposal_id=proposal_id
            )
        if event_id and activation is None:
            return _evolution_error("activation not found", reason="not_found", event_id=event_id)
        facts = {
            **_transition_facts("evolution_state", proposal_id or event_id or "latest", "observed"),
            "proposal": _proposal_facts(proposal) if proposal else {},
            "experiment": experiment.to_dict() if experiment else {},
            "activation": activation.to_dict() if activation else {},
            "active_observations": [
                item.to_dict()
                for item in experiments.list_activations(status="observing", limit=100)
            ],
            "targets": [
                asdict(item)
                for item in targets.descriptors()
            ],
            "available_eval_cases": list(targets.available_eval_cases()),
        }
        return _fact_result("evolution", facts)


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
            ledger = EvolutionLedger(self.home)
            proposal = ledger.get_proposal(proposal_id)
            if proposal is None:
                return _evolution_error(
                    "proposal not found", reason="not_found", proposal_id=proposal_id
                )
            if proposal.status == "proposed" and context.approved_approval_id:
                experiment = EvolutionExperimentStore(self.home).latest_experiment(
                    proposal_id
                )
                if experiment is None or experiment.status != "passed":
                    raise ValueError(
                        "proposal requires a passed persisted experiment before approval apply"
                    )
                ledger.record_proposal_evaluation(
                    proposal_id,
                    "approved",
                    evaluation_evidence=(
                        f"experiment_id={experiment.id} status={experiment.status}"
                    ),
                    approval_id=context.approved_approval_id,
                )
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
    return _failure_result(
        "evolution",
        message,
        error_reason=reason,
        facts={
            "reason": reason,
            "proposal_id": proposal_id,
            "event_id": event_id,
        },
    )


def _proposal_facts(proposal) -> dict[str, Any]:
    """Expose proposal lifecycle without leaking target payload through model tools."""
    data = asdict(proposal)
    before = str(data.pop("before", "") or "")
    after = str(data.pop("after", "") or "")
    data.pop("diff", None)
    try:
        data["eval_cases"] = json.loads(str(data.get("eval_cases") or "[]"))
    except json.JSONDecodeError:
        data["eval_cases"] = []
    data["baseline"] = {
        "sha256": hashlib.sha256(before.encode("utf-8")).hexdigest(),
        "characters": len(before),
    }
    data["candidate"] = {
        "sha256": hashlib.sha256(after.encode("utf-8")).hexdigest(),
        "characters": len(after),
    }
    return data


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _bounded_int(value: Any, *, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lower, min(upper, parsed))


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
