"""Regression tests for evolution and ledger security gates.

H4 — L0 approval bypass removed: every proposal now requires
     ``evaluation_result == 'approved'`` before it can be applied.
P1.2 — the ledger rejects undeclared target types instead of accepting schema
       drift silently.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from navi.capabilities_types import CapabilityContext
from navi.evolution import EvolutionLedger


def _ctx(home: Path, *, sender_id: str = "creator1") -> CapabilityContext:
    return CapabilityContext(
        home=home,
        peer_id="peer",
        sender_id=sender_id,
        source="connector.weixin",
        permission_ceiling="write",
        workspace=str(home),
    )


def test_h4_l0_proposal_still_requires_evaluation(tmp_path):
    """H4: the L0 bypass is gone. Even a proposal the model self-declares as
    L0 must have ``evaluation_result == 'approved'`` before apply."""
    ledger = EvolutionLedger(tmp_path)
    proposal = ledger.propose(
        target_type="prompt_layer",
        target_id="planner",
        reason="tighten routing",
        expected_benefit="fewer misroutes",
        risk="low",
        before="old",
        after="new",
        rollback_plan="revert file",
        required_approval_level="L0",
        source_run_id="run-1",
    )
    # Without an approved evaluation, apply must be refused.
    with pytest.raises(ValueError, match="evaluation_result='approved'"):
        ledger.assert_proposal_applicable(proposal)

    # After recording an approved evaluation, apply is permitted.
    ledger.record_proposal_evaluation(
        proposal.id, "approved", approver_id="user-1", approved_at=1.0
    )
    refreshed = ledger.get_proposal(proposal.id)
    ledger.assert_proposal_applicable(refreshed)  # no raise


def test_ledger_rejects_undeclared_target_type(tmp_path):
    """P1.2: the evolution ledger must reject ``target_type`` values that are
    neither declared evolution targets nor declared governance event types.
    Undeclared types are schema drift and must surface loudly."""
    ledger = EvolutionLedger(tmp_path)
    with pytest.raises(ValueError, match="unknown ledger target type"):
        ledger.record(
            run_id="run-1",
            target_type="totally_made_up",
            target_id="t1",
            reason="test",
            before="",
            after="",
        )


def test_ledger_accepts_governance_event_types(tmp_path):
    """Governance event types (execution_grant, approval) are declared and
    accepted by the ledger, alongside evolution target types."""
    ledger = EvolutionLedger(tmp_path)
    for governance_type in ("execution_grant", "approval"):
        event = ledger.record(
            run_id="run-1",
            target_type=governance_type,
            target_id="t1",
            reason="test",
            before="denied",
            after="allowed",
        )
        assert event.target_type == governance_type
