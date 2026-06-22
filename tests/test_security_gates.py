"""Regression tests for the security-gate fixes (Batch A).

H1 — workflow empty allowlist must deny every tool (deny by default), not
     short-circuit to "all tools allowed" when ``allowed_tools`` is empty.
H4 — L0 approval bypass removed: every proposal now requires
     ``evaluation_result == 'approved'`` before it can be applied.
M  — workflow approval requires the approver's sender_id to match the
     workflow creator; a different sender cannot approve a high-risk workflow.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from navi.actions.workflow import WorkflowRunCapability
from navi.capabilities_types import CapabilityContext
from navi.evolution import EvolutionLedger
from navi.tools import ToolSpec
from navi.workflows import WorkflowStore


def _ctx(home: Path, *, sender_id: str = "creator1") -> CapabilityContext:
    return CapabilityContext(
        home=home,
        peer_id="peer",
        sender_id=sender_id,
        source="connector.weixin",
        permission_ceiling="write",
        workspace=str(home),
    )


@pytest.mark.asyncio
async def test_h1_empty_allowlist_denies_tool(tmp_path):
    """A step with tool_calls but an empty allowed_tools list must reject
    every declared tool call, not silently allow them all."""
    store = WorkflowStore(tmp_path)
    workflow = store.create(
        objective="run a tool",
        workspace=str(tmp_path),
        source="cli",
        peer_id="peer",
        sender_id="creator1",
        permission_ceiling="read",
        max_concurrency=1,
        total_subagent_limit=1,
        risk_class="low",
        steps=[
            {
                "role": "planner",
                "objective": "call file.write",
                "allowed_tools": [],
                "tool_calls": [
                    {"tool": "file.write", "args": {}, "permission": "read"}
                ],
            }
        ],
    )
    step = store.list_steps(workflow.id)[0]
    cap = WorkflowRunCapability(
        ToolSpec(
            name="workflow.run",
            capability_class="workflow",
            execution_contexts=("turn",),
            description="run",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            facts_only=True,
            mutates=False,
            permission="read",
            source="action",
        ),
        home=tmp_path,
        project_dir=tmp_path,
    )
    result = await cap._run_step(store, workflow, step, context=_ctx(tmp_path))
    assert result.ok is False
    assert "not declared in step allowed_tools" in (result.message or result.observation)


@pytest.mark.asyncio
async def test_m_approver_must_match_creator(tmp_path):
    """Only the sender who created the workflow may approve it."""
    store = WorkflowStore(tmp_path)
    workflow = store.create(
        objective="test",
        workspace=str(tmp_path),
        source="cli",
        peer_id="peer",
        sender_id="creator1",
        permission_ceiling="read",
        max_concurrency=1,
        total_subagent_limit=1,
        risk_class="low",
    )
    from navi.actions.workflow import WorkflowApproveCapability

    spec = ToolSpec(
        name="workflow.approve",
        capability_class="workflow",
        execution_contexts=("turn",),
        description="approve",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        facts_only=True,
        mutates=False,
        permission="read",
        source="action",
    )
    cap = WorkflowApproveCapability(spec, home=tmp_path)

    intruder = await cap.invoke(
        {"workflow_id": workflow.id, "decision": "approve"},
        permission="read",
        context=_ctx(tmp_path, sender_id="intruder"),
    )
    assert intruder.ok is False
    assert intruder.error_reason == "approver_not_creator"

    creator = await cap.invoke(
        {"workflow_id": workflow.id, "decision": "approve"},
        permission="read",
        context=_ctx(tmp_path, sender_id="creator1"),
    )
    assert creator.ok is True


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
