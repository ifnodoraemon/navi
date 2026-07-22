from __future__ import annotations

import json
from pathlib import Path

from .evolution import EvolutionEvent, EvolutionLedger, EvolutionProposal
from .evolution_experiments import EvolutionExperimentStore
from .evolution_targets import EvolutionTargetAdapterRegistry


class EvolutionEngine:
    """Apply and roll back evaluated proposals through declared target adapters."""

    def __init__(self, home: Path):
        self.home = home
        self.ledger = EvolutionLedger(home)
        self.targets = EvolutionTargetAdapterRegistry(home)

    def apply_proposal(self, proposal_id: str) -> EvolutionEvent | None:
        proposal = self.ledger.get_proposal(proposal_id)
        if proposal is None:
            return None
        experiments = EvolutionExperimentStore(self.home)
        if proposal.status == "applied" and proposal.applied_event_id:
            event = self.ledger.get(proposal.applied_event_id)
            if event is not None:
                experiments.start_activation(proposal_id=proposal.id, event_id=event.id)
            return event
        if proposal.status == "applying":
            return self._reconcile_applying(proposal)
        self.ledger.assert_proposal_applicable(proposal)
        if json.loads(proposal.eval_cases or "[]"):
            experiments.assert_passed(proposal.id, candidate=proposal.after)
        adapter = self.targets.get(proposal.target_type)
        current = adapter.read(proposal.target_id)
        if current != proposal.before:
            raise ValueError(
                "evolution proposal baseline is stale; create a new proposal from current state"
            )
        adapter.validate(proposal.target_id, proposal.after)
        proposal = self.ledger.claim_for_apply(proposal_id)
        current = adapter.read(proposal.target_id)
        if current != proposal.before:
            self.ledger.mark_apply_failed(proposal_id)
            raise ValueError(
                "evolution proposal baseline changed while apply was being claimed"
            )
        snapshot = getattr(adapter, "snapshot", None)
        rollback_state = snapshot(proposal.target_id) if callable(snapshot) else current
        event = self.ledger.record_apply_event(proposal, rollback_state=rollback_state)
        try:
            adapter.apply(proposal.target_id, proposal.after)
        except Exception as exc:
            self.ledger.record(
                run_id=proposal.source_run_id,
                target_type=proposal.target_type,
                target_id=proposal.target_id,
                reason="proposal_apply_side_effect_failed",
                before=event.after,
                after=json.dumps(
                    {"error_type": type(exc).__name__, "error": str(exc)}, sort_keys=True
                ),
                proposal_id=proposal.id,
            )
            self.ledger.mark_apply_failed(proposal_id)
            raise
        self.ledger.mark_applied(proposal_id, event.id)
        experiments.start_activation(proposal_id=proposal.id, event_id=event.id)
        return event

    def _reconcile_applying(self, proposal: EvolutionProposal) -> EvolutionEvent:
        adapter = self.targets.get(proposal.target_type)
        current = adapter.read(proposal.target_id)
        event = self.ledger.apply_event_for_proposal(proposal.id)
        if current == proposal.after and event is not None:
            self.ledger.mark_applied(proposal.id, event.id)
            EvolutionExperimentStore(self.home).start_activation(
                proposal_id=proposal.id,
                event_id=event.id,
            )
            return event
        if current == proposal.before:
            self.ledger.mark_apply_failed(proposal.id)
            raise ValueError("interrupted evolution apply did not land; create a new proposal")
        self.ledger.mark_apply_uncertain(proposal.id)
        raise ValueError(
            "interrupted evolution apply has an uncertain target state; manual review required"
        )

    def rollback(self, event_id: str) -> EvolutionEvent | None:
        def _restore(event: EvolutionEvent) -> None:
            adapter = self.targets.get(event.target_type)
            rollback_snapshot = getattr(adapter, "rollback_snapshot", None)
            if callable(rollback_snapshot):
                rollback_snapshot(event.target_id, event.rollback_state)
            else:
                adapter.rollback(event.target_id, event.rollback_state)

        event = self.ledger.rollback_applied_event(event_id, _restore)
        if event is None:
            return None
        EvolutionExperimentStore(self.home).mark_rolled_back(
            event_id,
            rollback_event_id=event_id,
        )
        return event
