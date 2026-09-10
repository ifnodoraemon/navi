from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from navi.actions.evolution import (
    EvolutionApplyCapability,
    EvolutionCandidatesCapability,
    EvolutionExperimentCapability,
    EvolutionObserveCapability,
    EvolutionProposeCapability,
    EvolutionRecordEvaluationCapability,
    EvolutionRollbackCapability,
    EvolutionStateCapability,
    _bounded_int,
    _evolution_error,
    _nonnegative_int,
    _proposal_facts,
    _string_list,
)
from navi.capabilities_types import CapabilityContext
from navi.evolution import EvolutionProposal
from navi.runs import RunStore
from navi.tools import ToolSpec

_DUMMY_SPEC = ToolSpec(
    name="evolution_test",
    capability_class="evolution",
    execution_contexts=("turn",),
    description="evolution test tool",
    input_schema={},
    output_schema={},
)


def test_helpers():
    assert _bounded_int("10", default=5, lower=1, upper=20) == 10
    assert _bounded_int("invalid", default=5, lower=1, upper=20) == 5
    assert _bounded_int(50, default=5, lower=1, upper=20) == 20
    assert _bounded_int(-5, default=5, lower=1, upper=20) == 1

    assert _nonnegative_int("12") == 12
    assert _nonnegative_int("-5") == 0
    assert _nonnegative_int("abc") == 0

    assert _string_list(["a", "b"]) == ["a", "b"]
    assert _string_list("not_list") == []

    err = _evolution_error("test error", reason="test_reason", proposal_id="p1")
    assert err.ok is False
    assert err.facts["reason"] == "test_reason"

    prop = EvolutionProposal(
        id="prop-test",
        target_type="prompt",
        target_id="planner",
        reason="better prompts",
        expected_benefit="pass rate",
        risk="low",
        before="old text",
        after="new text",
        diff="diff text",
        rollback_plan="restore",
        required_approval_level="L2",
        evidence="eval logs",
        source_run_id="run-1",
        status="proposed",
        created_at=123.0,
        applied_at=0.0,
        applied_event_id="",
        eval_cases='["test1"]',
        evaluation_result="pending",
    )
    facts = _proposal_facts(prop)
    assert facts["id"] == "prop-test"
    assert facts["eval_cases"] == ["test1"]
    assert "sha256" in facts["baseline"]

    bad_prop = EvolutionProposal(
        id="prop-bad",
        target_type="prompt",
        target_id="planner",
        reason="test",
        expected_benefit="test",
        risk="low",
        before="old",
        after="new",
        diff="diff",
        rollback_plan="restore",
        required_approval_level="L2",
        evidence="logs",
        source_run_id="run-1",
        status="proposed",
        created_at=123.0,
        applied_at=0.0,
        applied_event_id="",
        eval_cases="not-json",
        evaluation_result="pending",
    )
    bad_facts = _proposal_facts(bad_prop)
    assert bad_facts["eval_cases"] == []


@pytest.mark.asyncio
async def test_evolution_candidates_capability(tmp_path: Path):
    cap = EvolutionCandidatesCapability(_DUMMY_SPEC, home=tmp_path)
    ctx = CapabilityContext(home=tmp_path, peer_id="test", sender_id="test", source="cli")
    res = await cap.invoke({}, permission="read", context=ctx)
    assert res.ok is True
    assert "candidates" in res.facts


@pytest.mark.asyncio
async def test_evolution_propose_capability(tmp_path: Path):
    cap = EvolutionProposeCapability(_DUMMY_SPEC, home=tmp_path)
    ctx = CapabilityContext(home=tmp_path, peer_id="test", sender_id="test", source="cli")

    res_err = await cap.invoke({"target_type": "unknown_type"}, permission="write", context=ctx)
    assert res_err.ok is False
    assert res_err.facts["reason"] == "schema_mismatch"

    res_ok = await cap.invoke(
        {
            "target_type": "prompt_layer",
            "target_id": "instructions",
            "reason": "Defense enhancement",
            "after": "Always produce structured tool calls and respect system constraints.\n",
            "expected_benefit": "Less prompt injection",
            "rollback_plan": "Restore original prompt",
            "eval_cases": ["runtime.text.nonempty"],
        },
        permission="write",
        context=ctx,
    )
    assert res_ok.ok is True
    assert "proposal_id" in res_ok.facts


@pytest.mark.asyncio
async def test_evolution_record_evaluation_and_experiment(tmp_path: Path):
    ctx = CapabilityContext(home=tmp_path, peer_id="test", sender_id="test", source="cli")

    rec_cap = EvolutionRecordEvaluationCapability(_DUMMY_SPEC, home=tmp_path)
    res_no_args = await rec_cap.invoke({}, permission="write", context=ctx)
    assert res_no_args.ok is False

    res_not_found = await rec_cap.invoke(
        {"proposal_id": "nonexistent", "evaluation_result": "passed"},
        permission="write",
        context=ctx,
    )
    assert res_not_found.ok is False

    # Create a proposal first
    prop_cap = EvolutionProposeCapability(_DUMMY_SPEC, home=tmp_path)
    prop_res = await prop_cap.invoke(
        {
            "target_type": "prompt_layer",
            "target_id": "instructions",
            "reason": "Defense enhancement",
            "after": "Always produce structured tool calls and respect system constraints. Extra safety rules.\n",
            "expected_benefit": "Less prompt injection",
            "rollback_plan": "Restore original prompt",
            "eval_cases": ["runtime.text.nonempty"],
        },
        permission="write",
        context=ctx,
    )
    assert prop_res.ok is True
    prop_id = prop_res.facts["proposal_id"]

    # Record evaluation rejection
    rec_res = await rec_cap.invoke(
        {"proposal_id": prop_id, "evaluation_result": "rejected"},
        permission="write",
        context=ctx,
    )
    assert rec_res.ok is True
    assert rec_res.facts["proposal"]["evaluation_result"] == "rejected"

    exp_cap = EvolutionExperimentCapability(_DUMMY_SPEC, home=tmp_path)
    res_exp_no_id = await exp_cap.invoke({}, permission="write", context=ctx)
    assert res_exp_no_id.ok is False

    res_exp_not_found = await exp_cap.invoke(
        {"proposal_id": "nonexistent"},
        permission="write",
        context=ctx,
    )
    assert res_exp_not_found.ok is False

    exp_res = await exp_cap.invoke({"proposal_id": prop_id}, permission="write", context=ctx)
    assert exp_res.ok is True
    assert exp_res.facts["experiment"]["status"] == "passed"


@pytest.mark.asyncio
async def test_evolution_observe_and_state(tmp_path: Path):
    ctx = CapabilityContext(home=tmp_path, peer_id="test", sender_id="test", source="cli")

    obs_cap = EvolutionObserveCapability(_DUMMY_SPEC, home=tmp_path)
    res_obs_no_id = await obs_cap.invoke({}, permission="write", context=ctx)
    assert res_obs_no_id.ok is False

    res_obs_not_found = await obs_cap.invoke(
        {"event_id": "nonexistent"}, permission="write", context=ctx
    )
    assert res_obs_not_found.ok is False

    state_cap = EvolutionStateCapability(_DUMMY_SPEC, home=tmp_path)
    res_state = await state_cap.invoke({}, permission="read", context=ctx)
    assert res_state.ok is True
    assert "targets" in res_state.facts

    res_state_missing = await state_cap.invoke(
        {"proposal_id": "nonexistent"}, permission="read", context=ctx
    )
    assert res_state_missing.ok is False


@pytest.mark.asyncio
async def test_evolution_apply_and_rollback(tmp_path: Path):
    # 1. Propose
    ctx_cli = CapabilityContext(home=tmp_path, peer_id="test", sender_id="test", source="cli")
    prop_cap = EvolutionProposeCapability(_DUMMY_SPEC, home=tmp_path)
    prop_res = await prop_cap.invoke(
        {
            "target_type": "prompt_layer",
            "target_id": "instructions",
            "reason": "Defense enhancement",
            "after": "Always produce structured tool calls and respect system constraints. Extra safety.\n",
            "expected_benefit": "Less prompt injection",
            "rollback_plan": "Restore original prompt",
            "eval_cases": ["runtime.text.nonempty"],
        },
        permission="write",
        context=ctx_cli,
    )
    assert prop_res.ok is True
    prop_id = prop_res.facts["proposal_id"]

    # 2. Experiment
    exp_cap = EvolutionExperimentCapability(_DUMMY_SPEC, home=tmp_path)
    exp_res = await exp_cap.invoke({"proposal_id": prop_id}, permission="write", context=ctx_cli)
    assert exp_res.ok is True

    # 3. Create and resolve approval for evolution.apply
    run_store = RunStore(tmp_path)
    approval = run_store.create_approval(
        run_id="run-1",
        action="evolution.apply",
        requested_tool="evolution.apply",
        args_json=json.dumps({"proposal_id": prop_id}),
        reason="test approval",
    )
    run_store.resolve_approval(approval.id, decision="approve", resolved_by="admin")

    ctx_approved = CapabilityContext(
        home=tmp_path,
        peer_id="test",
        sender_id="test",
        source="cli",
        approved_approval_id=approval.id,
    )

    # 4. Apply
    apply_cap = EvolutionApplyCapability(_DUMMY_SPEC, home=tmp_path)
    res_apply_no_id = await apply_cap.invoke({}, permission="write", context=ctx_approved)
    assert res_apply_no_id.ok is False

    res_apply_not_found = await apply_cap.invoke(
        {"proposal_id": "nonexistent"}, permission="write", context=ctx_approved
    )
    assert res_apply_not_found.ok is False

    apply_res = await apply_cap.invoke(
        {"proposal_id": prop_id}, permission="write", context=ctx_approved
    )
    assert apply_res.ok is True
    event_id = apply_res.facts["event_id"]

    # 5. Observe
    obs_cap = EvolutionObserveCapability(_DUMMY_SPEC, home=tmp_path)
    obs_res = await obs_cap.invoke(
        {"event_id": event_id, "successes": 5, "errors": 0, "evidence": {"status": "ok"}},
        permission="write",
        context=ctx_approved,
    )
    assert obs_res.ok is True

    # 6. State with proposal & event
    state_cap = EvolutionStateCapability(_DUMMY_SPEC, home=tmp_path)
    state_res = await state_cap.invoke(
        {"proposal_id": prop_id, "event_id": event_id},
        permission="read",
        context=ctx_approved,
    )
    assert state_res.ok is True
    assert state_res.facts["proposal"]["id"] == prop_id

    # 7. Rollback
    rb_cap = EvolutionRollbackCapability(_DUMMY_SPEC, home=tmp_path)
    res_rb_no_id = await rb_cap.invoke({}, permission="write", context=ctx_approved)
    assert res_rb_no_id.ok is False

    res_rb_not_found = await rb_cap.invoke(
        {"event_id": "nonexistent"}, permission="write", context=ctx_approved
    )
    assert res_rb_not_found.ok is False

    rb_res = await rb_cap.invoke({"event_id": event_id}, permission="write", context=ctx_approved)
    assert rb_res.ok is True
    assert rb_res.facts["event"]["rolled_back_at"] > 0
