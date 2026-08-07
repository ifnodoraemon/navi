from __future__ import annotations

import json
from pathlib import Path

import pytest

from navi.approval_contract import APPROVAL_ACTION_EVOLUTION, APPROVAL_DECISION_APPROVE
from navi.capabilities import build_capability_registry
from navi.capabilities_types import CapabilityContext
from navi.evolution import EvolutionLedger
from navi.evolution_engine import EvolutionEngine
from navi.evolution_experiments import EvolutionExperimentStore
from navi.evolution_targets import EvolutionTargetAdapterRegistry
from navi.memory import MemoryStore
from navi.prompting import PromptLayerStore
from navi.runs import RunStore
from navi.tools import API_CONTEXT


@pytest.mark.asyncio
async def test_model_evolution_can_bootstrap_from_runtime_eval_contracts(
    tmp_path: Path,
) -> None:
    registry = build_capability_registry(tmp_path, project_dir=tmp_path)
    context = CapabilityContext(
        home=tmp_path,
        loop_run_id="loop-model-evolution",
        trace_id="trace-model-evolution",
        source="cli",
        peer_id="cli",
        sender_id="cli",
        workspace=str(tmp_path),
    )

    state = await registry.invoke(
        "evolution.state",
        {},
        permission="read",
        context=context,
    )
    assert state.ok is True
    cases = {
        item["id"]: item
        for item in state.facts["available_eval_cases"]
    }
    assert cases["runtime.text.nonempty"] == {
        "id": "runtime.text.nonempty",
        "target_types": ["prompt_layer", "skill"],
        "assertion_types": ["nonempty"],
        "source": "runtime",
        "mutable": False,
    }

    before = PromptLayerStore(tmp_path).read("identity")
    candidate = before + "\nPrefer auditable evolution proposals.\n"
    proposed = await registry.invoke(
        "evolution.propose",
        {
            "target_type": "prompt_layer",
            "target_id": "identity",
            "reason": "exercise model-owned evolution",
            "expected_benefit": "reviewable behavior improvement",
            "risk": "planner behavior change",
            "after": candidate,
            "rollback_plan": "restore the authoritative prompt baseline",
            "evidence": "repeated trace evidence",
            "source_run_id": "spoofed-run-id",
            "eval_cases": ["runtime.text.nonempty"],
        },
        permission="prepare",
        context=context,
    )
    assert proposed.error_reason == "sensitive_op_requires_approval"
    approved = await registry.invoke(
        "approval.resolve",
        {
            "decision": "approve",
            "code": proposed.facts["approval"]["code"],
        },
        permission="write",
        context=context,
    )
    assert approved.ok is True
    proposed = await registry.invoke(
        "evolution.propose",
        {
            "target_type": "prompt_layer",
            "target_id": "identity",
            "reason": "exercise model-owned evolution",
            "expected_benefit": "reviewable behavior improvement",
            "risk": "planner behavior change",
            "after": candidate,
            "rollback_plan": "restore the authoritative prompt baseline",
            "evidence": "repeated trace evidence",
            "source_run_id": "spoofed-run-id",
            "eval_cases": ["runtime.text.nonempty"],
        },
        permission="prepare",
        context=context,
    )

    assert proposed.ok is True
    proposal_id = proposed.facts["proposal_id"]
    proposal = EvolutionLedger(tmp_path).get_proposal(proposal_id)
    assert proposal is not None
    assert proposal.before == before
    assert proposal.after == candidate
    assert proposal.source_run_id == "loop-model-evolution"
    assert "before" not in proposed.facts["proposal"]
    assert "after" not in proposed.facts["proposal"]
    assert proposed.facts["target_validation"]["loaded_by"] == "PromptLayerStore"

    experiment = await registry.invoke(
        "evolution.experiment",
        {"proposal_id": proposal_id},
        permission="prepare",
        context=context,
    )
    assert experiment.error_reason == "sensitive_op_requires_approval"
    approved = await registry.invoke(
        "approval.resolve",
        {
            "decision": "approve",
            "code": experiment.facts["approval"]["code"],
        },
        permission="write",
        context=context,
    )
    assert approved.ok is True
    experiment = await registry.invoke(
        "evolution.experiment",
        {"proposal_id": proposal_id},
        permission="prepare",
        context=context,
    )

    assert experiment.ok is True
    assert experiment.facts["experiment"]["status"] == "passed"
    assert PromptLayerStore(tmp_path).read("identity") == before


def test_evolution_experiment_activation_and_regression_rollback(tmp_path: Path) -> None:
    prompts = PromptLayerStore(tmp_path)
    before = prompts.read("identity")
    after = before + "\nPrefer the smallest sufficient capability set.\n"
    eval_case = {
        "id": "planner-safety",
        "target_types": ["prompt_layer"],
        "assertions": [
            {"type": "contains", "value": "smallest sufficient capability"},
            {"type": "not_contains", "value": "ignore approvals"},
        ],
    }
    EvolutionTargetAdapterRegistry(tmp_path).get("eval_case").apply(
        "planner-safety",
        json.dumps(eval_case, sort_keys=True),
    )
    ledger = EvolutionLedger(tmp_path)
    proposal = ledger.propose(
        target_type="prompt_layer",
        target_id="identity",
        reason="reduce capability overreach",
        expected_benefit="fewer unnecessarily broad plans",
        risk="routing regression",
        before=before,
        after=after,
        rollback_plan="restore the previous prompt layer",
        source_run_id="run-evolution",
        eval_cases=["planner-safety"],
    )

    experiment = EvolutionExperimentStore(tmp_path).run(proposal.id)
    assert experiment.status == "passed"

    runs = RunStore(tmp_path)
    approval_run = runs.create("Approve tested evolution", workspace=str(tmp_path))
    approval = runs.create_approval(
        run_id=approval_run.id,
        action=APPROVAL_ACTION_EVOLUTION,
        requested_tool="evolution.apply",
        requested_permission="write",
        args_json=json.dumps({"proposal_id": proposal.id}),
        reason="apply tested prompt proposal",
    )
    runs.resolve_approval(
        approval.id,
        decision=APPROVAL_DECISION_APPROVE,
        resolved_by="user-1",
    )
    ledger.record_proposal_evaluation(
        proposal.id,
        "approved",
        evaluation_evidence=f"experiment_id={experiment.id} status=passed",
        approval_id=approval.id,
    )

    evaluation_event = ledger.list(limit=1)[0]
    assert evaluation_event.event_kind == "audit"
    with pytest.raises(ValueError, match="only successful evolution apply events"):
        EvolutionEngine(tmp_path).rollback(evaluation_event.id)
    assert prompts.read("identity") == before

    event = EvolutionEngine(tmp_path).apply_proposal(proposal.id)
    assert event is not None
    assert event.event_kind == "apply"
    assert EvolutionEngine(tmp_path).apply_proposal(proposal.id).id == event.id
    assert prompts.read("identity") == after
    activations = EvolutionExperimentStore(tmp_path)
    assert activations.activation_for_event(event.id).status == "observing"

    rollback = EvolutionEngine(tmp_path).rollback
    activations.observe(
        event.id, successes=0, errors=1, evidence={"window": 1}, rollback=rollback
    )
    activations.observe(
        event.id, successes=0, errors=1, evidence={"window": 2}, rollback=rollback
    )
    rolled_back = activations.observe(
        event.id,
        successes=0,
        errors=1,
        evidence={"window": 3},
        rollback=rollback,
    )

    assert rolled_back.status == "rolled_back"
    assert prompts.read("identity") == before
    assert prompts.override_path("identity").exists() is False
    assert ledger.get(event.id).rolled_back_at > 0


def test_unloaded_spec_file_targets_are_not_declared_as_evolution(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown evolution target type"):
        EvolutionLedger(tmp_path).propose(
            target_type="tool_spec",
            target_id="unsafe",
            reason="would only write an inert file",
            expected_benefit="none",
            risk="false confidence",
            before="",
            after="name: unsafe",
            rollback_plan="delete inert file",
            eval_cases=["meta-eval"],
        )


def test_prompt_evolution_rejects_layers_not_consumed_by_runtime(tmp_path: Path) -> None:
    adapter = EvolutionTargetAdapterRegistry(tmp_path).get("prompt_layer")

    with pytest.raises(ValueError, match="not loaded by the runtime"):
        adapter.validate("planner", "This file would be inert.")


def test_skill_evolution_requires_a_loadable_instruction_contract(tmp_path: Path) -> None:
    adapter = EvolutionTargetAdapterRegistry(tmp_path).get("skill")

    for invalid in (
        "plain text only",
        "---\nname: demo\n---\nInstructions",
        "---\nname: demo\ndescription: Demo\npermission: owner\n---\nInstructions",
        "---\nname: demo\ndescription: Demo\npermission: read\n---\n",
    ):
        with pytest.raises(ValueError, match="skill candidate"):
            adapter.validate("demo", invalid)

    candidate = (
        "---\n"
        "name: Demo\n"
        "description: Demonstrate a governed procedure.\n"
        "permission: read\n"
        "---\n"
        "# Demo\n\nFollow the declared capability contracts.\n"
    )
    facts = adapter.validate("demo", candidate)

    assert facts == {
        "loaded_by": "SkillStore",
        "characters": len(candidate),
        "name": "Demo",
        "description_characters": len("Demonstrate a governed procedure."),
        "permission": "read",
        "instructions_present": True,
    }


def test_manual_rollback_rejects_an_audit_event(tmp_path: Path) -> None:
    prompts = PromptLayerStore(tmp_path)
    before = prompts.read("identity")
    after = before + "\nPrefer reversible changes.\n"
    event = EvolutionLedger(tmp_path).record(
        run_id="run-manual-rollback",
        target_type="prompt_layer",
        target_id="identity",
        reason="exercise explicit rollback",
        before=before,
        after=after,
    )
    EvolutionTargetAdapterRegistry(tmp_path).get("prompt_layer").apply("identity", after)
    activations = EvolutionExperimentStore(tmp_path)
    activations.start_activation(proposal_id="proposal-manual", event_id=event.id)

    with pytest.raises(ValueError, match="only successful evolution apply events"):
        EvolutionEngine(tmp_path).rollback(event.id)

    assert prompts.read("identity") == after
    activation = activations.activation_for_event(event.id)
    assert activation is not None
    assert activation.status == "observing"


def test_interrupted_apply_is_reconciled_without_reexecuting_target(tmp_path: Path) -> None:
    prompts = PromptLayerStore(tmp_path)
    before = prompts.read("identity")
    after = before + "\nRecover interrupted apply.\n"
    ledger = EvolutionLedger(tmp_path)
    proposal = ledger.propose(
        target_type="prompt_layer",
        target_id="identity",
        reason="recovery test",
        expected_benefit="crash recovery",
        risk="behavior change",
        before=before,
        after=after,
        rollback_plan="restore prompt",
        eval_cases=["recovery-eval"],
    )
    claimed = ledger.claim_for_apply(proposal.id)
    adapter = EvolutionTargetAdapterRegistry(tmp_path).get("prompt_layer")
    event = ledger.record_apply_event(
        claimed,
        rollback_state=adapter.snapshot("identity"),
    )
    adapter.apply("identity", after)

    reconciled = EvolutionEngine(tmp_path).apply_proposal(proposal.id)

    assert reconciled.id == event.id
    assert ledger.get_proposal(proposal.id).status == "applied"
    assert prompts.read("identity") == after


def test_changed_eval_case_invalidates_a_persisted_experiment(tmp_path: Path) -> None:
    prompts = PromptLayerStore(tmp_path)
    before = prompts.read("identity")
    after = before + "\nUse bounded plans.\n"
    eval_adapter = EvolutionTargetAdapterRegistry(tmp_path).get("eval_case")
    eval_adapter.apply(
        "bounded-plan",
        json.dumps(
            {
                "id": "bounded-plan",
                "target_types": ["prompt_layer"],
                "assertions": [{"type": "contains", "value": "bounded plans"}],
            },
            sort_keys=True,
        ),
    )
    proposal = EvolutionLedger(tmp_path).propose(
        target_type="prompt_layer",
        target_id="identity",
        reason="bound planning",
        expected_benefit="bounded plans",
        risk="behavior change",
        before=before,
        after=after,
        rollback_plan="restore prompt",
        eval_cases=["bounded-plan"],
    )
    experiments = EvolutionExperimentStore(tmp_path)
    assert experiments.run(proposal.id).status == "passed"

    eval_adapter.apply(
        "bounded-plan",
        json.dumps(
            {
                "id": "bounded-plan",
                "target_types": ["prompt_layer"],
                "assertions": [{"type": "not_contains", "value": "bounded plans"}],
            },
            sort_keys=True,
        ),
    )

    with pytest.raises(ValueError, match="evaluation case changed"):
        experiments.assert_passed(proposal.id, candidate=after)


def test_memory_evolution_preserves_scope_lifecycle_and_exact_rollback(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    item = store.add_item(
        "preference",
        "concise",
        source="user",
        status="active",
        scope="actor:one",
        confidence=0.9,
        reason="explicit",
        provenance="test",
    )
    adapter = EvolutionTargetAdapterRegistry(tmp_path).get("memory_item")
    before = adapter.read(item.id)
    changed = json.loads(before)
    changed["content"] = "concise and factual"
    changed["confidence"] = 0.8
    adapter.apply(item.id, json.dumps(changed, sort_keys=True))
    adapter.rollback(item.id, before)

    restored = store.get_item(item.id)
    assert restored is not None
    assert restored.confidence == 0.9
    escaped = json.loads(before)
    escaped["scope"] = "global"
    with pytest.raises(ValueError, match="authority or lifecycle"):
        adapter.validate(item.id, json.dumps(escaped, sort_keys=True))


@pytest.mark.asyncio
async def test_evolution_apply_and_manual_rollback_require_capability_approval(
    tmp_path: Path,
) -> None:
    prompts = PromptLayerStore(tmp_path)
    before = prompts.read("identity")
    proposal = EvolutionLedger(tmp_path).propose(
        target_type="prompt_layer",
        target_id="identity",
        reason="governed apply",
        expected_benefit="safer changes",
        risk="behavior change",
        before=before,
        after=before + "\nPrefer governed evolution.\n",
        rollback_plan="restore prompt",
        eval_cases=["governed-eval"],
    )
    EvolutionTargetAdapterRegistry(tmp_path).get("eval_case").apply(
        "governed-eval",
        json.dumps(
            {
                "id": "governed-eval",
                "target_types": ["prompt_layer"],
                "assertions": [
                    {"type": "contains", "value": "governed evolution"}
                ],
            },
            sort_keys=True,
        ),
    )
    assert EvolutionExperimentStore(tmp_path).run(proposal.id).status == "passed"
    registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        execution_context=API_CONTEXT,
    )
    context = CapabilityContext(
        home=tmp_path,
        source="cli",
        peer_id="cli",
        sender_id="cli",
        workspace=str(tmp_path),
        permission_ceiling="write",
    )

    requested = await registry.invoke(
        "evolution.apply",
        {"proposal_id": proposal.id},
        permission="write",
        context=context,
    )
    assert requested.ok is False
    approval_id = requested.facts["approval"]["id"]
    RunStore(tmp_path).resolve_approval(
        approval_id,
        decision=APPROVAL_DECISION_APPROVE,
        resolved_by="user-1",
    )
    governed_registry = build_capability_registry(
        tmp_path,
        project_dir=tmp_path,
        execution_context=API_CONTEXT,
        governed_run_id=RunStore(tmp_path).get_approval(approval_id).run_id,
    )

    applied = await governed_registry.invoke(
        "evolution.apply",
        {"proposal_id": proposal.id},
        permission="write",
        context=context,
    )
    assert applied.ok is True
    rollback_requested = await registry.invoke(
        "evolution.rollback",
        {"event_id": applied.facts["event_id"]},
        permission="write",
        context=context,
    )
    assert rollback_requested.ok is False
    assert rollback_requested.facts["approval"]["requested_tool"] == "evolution.rollback"
