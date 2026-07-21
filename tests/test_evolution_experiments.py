from __future__ import annotations

import json
from pathlib import Path

import pytest

from navi.approval_contract import APPROVAL_ACTION_EVOLUTION, APPROVAL_DECISION_APPROVE
from navi.evolution import EvolutionEngine, EvolutionLedger
from navi.evolution_experiments import EvolutionExperimentStore
from navi.evolution_targets import EvolutionTargetAdapterRegistry
from navi.prompting import PromptLayerStore
from navi.runs import RunStore


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

    event = EvolutionEngine(tmp_path).apply_proposal(proposal.id)
    assert event is not None
    assert prompts.read("planner") == after
    activations = EvolutionExperimentStore(tmp_path)
    assert activations.activation_for_event(event.id).status == "observing"

    activations.observe(event.id, successes=0, errors=1, evidence={"window": 1})
    activations.observe(event.id, successes=0, errors=1, evidence={"window": 2})
    rolled_back = activations.observe(
        event.id,
        successes=0,
        errors=1,
        evidence={"window": 3},
    )

    assert rolled_back.status == "rolled_back"
    assert prompts.read("planner") == before
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
        )


def test_manual_rollback_closes_activation_state(tmp_path: Path) -> None:
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

    rolled_back = EvolutionEngine(tmp_path).rollback(event.id)

    assert rolled_back is not None
    assert rolled_back.rolled_back_at > 0
    assert prompts.read("planner") == before
    activation = activations.activation_for_event(event.id)
    assert activation is not None
    assert activation.status == "rolled_back"
    assert activation.rollback_event_id == event.id
