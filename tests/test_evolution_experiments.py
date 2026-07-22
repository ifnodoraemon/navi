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


def test_evolution_experiment_activation_and_regression_rollback(tmp_path: Path) -> None:
    prompts = PromptLayerStore(tmp_path)
    before = prompts.read("planner")
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
        target_id="planner",
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
    assert prompts.read("planner") == before

    event = EvolutionEngine(tmp_path).apply_proposal(proposal.id)
    assert event is not None
    assert event.event_kind == "apply"
    assert EvolutionEngine(tmp_path).apply_proposal(proposal.id).id == event.id
    assert prompts.read("planner") == after
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
    assert prompts.read("planner") == before
    assert prompts.override_path("planner").exists() is False
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


def test_manual_rollback_rejects_an_audit_event(tmp_path: Path) -> None:
    prompts = PromptLayerStore(tmp_path)
    before = prompts.read("planner")
    after = before + "\nPrefer reversible changes.\n"
    event = EvolutionLedger(tmp_path).record(
        run_id="run-manual-rollback",
        target_type="prompt_layer",
        target_id="planner",
        reason="exercise explicit rollback",
        before=before,
        after=after,
    )
    EvolutionTargetAdapterRegistry(tmp_path).get("prompt_layer").apply("planner", after)
    activations = EvolutionExperimentStore(tmp_path)
    activations.start_activation(proposal_id="proposal-manual", event_id=event.id)

    with pytest.raises(ValueError, match="only successful evolution apply events"):
        EvolutionEngine(tmp_path).rollback(event.id)

    assert prompts.read("planner") == after
    activation = activations.activation_for_event(event.id)
    assert activation is not None
    assert activation.status == "observing"


def test_interrupted_apply_is_reconciled_without_reexecuting_target(tmp_path: Path) -> None:
    prompts = PromptLayerStore(tmp_path)
    before = prompts.read("planner")
    after = before + "\nRecover interrupted apply.\n"
    ledger = EvolutionLedger(tmp_path)
    proposal = ledger.propose(
        target_type="prompt_layer",
        target_id="planner",
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
        rollback_state=adapter.snapshot("planner"),
    )
    adapter.apply("planner", after)

    reconciled = EvolutionEngine(tmp_path).apply_proposal(proposal.id)

    assert reconciled.id == event.id
    assert ledger.get_proposal(proposal.id).status == "applied"
    assert prompts.read("planner") == after


def test_changed_eval_case_invalidates_a_persisted_experiment(tmp_path: Path) -> None:
    prompts = PromptLayerStore(tmp_path)
    before = prompts.read("planner")
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
        target_id="planner",
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
    before = prompts.read("planner")
    proposal = EvolutionLedger(tmp_path).propose(
        target_type="prompt_layer",
        target_id="planner",
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
