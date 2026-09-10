"""Tests for Self-Play Shadow Arena and autonomous parameter exploration."""
from __future__ import annotations

import json
from pathlib import Path

from navi.dynamic_parameters import DynamicParameterRegistry
from navi.prompting import PromptLayerStore
from navi.self_play import (
    SELF_PLAY_TRIALS_TABLE,
    SelfPlayArena,
    ShadowTrialSpec,
)


def test_generate_parameter_perturbations(tmp_path: Path) -> None:
    arena = SelfPlayArena(tmp_path)
    specs = arena.generate_parameter_perturbations(limit=4)
    assert len(specs) > 0
    assert len(specs) <= 4
    for spec in specs:
        assert spec.target_type == "dynamic_parameter"
        assert spec.baseline_value != spec.candidate_value
        assert "exploratory" in spec.hypothesis


def test_execute_shadow_trial_success_and_promotion(tmp_path: Path) -> None:
    arena = SelfPlayArena(tmp_path)
    param_reg = DynamicParameterRegistry(tmp_path)
    initial_cov = param_reg.get("cue_weight_coverage")
    assert initial_cov == 0.60

    spec = ShadowTrialSpec(
        trial_id="trial_1",
        target_type="dynamic_parameter",
        target_id="cue_weight_coverage",
        baseline_value=initial_cov,
        candidate_value=0.65,
        hypothesis="explore_higher_coverage",
    )

    result = arena.execute_shadow_trial(spec, auto_promote=True)
    assert result.passed
    assert result.promoted
    assert result.score_delta > 0.0
    assert result.candidate_value == 0.65

    # Verify parameter updated in registry
    updated_cov = param_reg.get("cue_weight_coverage", reload=True)
    assert updated_cov == 0.65

    # Verify trial record persisted
    trials = arena.list_trials(target_id="cue_weight_coverage")
    assert len(trials) == 1
    assert trials[0].promoted
    assert trials[0].candidate_value == 0.65


def test_execute_shadow_trial_without_promotion(tmp_path: Path) -> None:
    arena = SelfPlayArena(tmp_path)
    param_reg = DynamicParameterRegistry(tmp_path)
    initial_jaccard = param_reg.get("cue_weight_jaccard")

    spec = ShadowTrialSpec(
        trial_id="trial_2",
        target_type="dynamic_parameter",
        target_id="cue_weight_jaccard",
        baseline_value=initial_jaccard,
        candidate_value=0.30,
        hypothesis="dry_run_jaccard_boost",
    )

    result = arena.execute_shadow_trial(spec, auto_promote=False)
    assert result.passed
    assert not result.promoted

    # Verify parameter remained unchanged
    unchanged_jaccard = param_reg.get("cue_weight_jaccard", reload=True)
    assert unchanged_jaccard == initial_jaccard


def test_run_autonomous_cycle(tmp_path: Path) -> None:
    arena = SelfPlayArena(tmp_path)
    results = arena.run_autonomous_cycle(max_trials=3, auto_promote=True)
    assert len(results) > 0
    assert len(results) <= 3
    for res in results:
        assert res.passed
        assert res.promoted
        assert res.score_delta > 0.0

    promoted_trials = arena.list_trials(promoted_only=True)
    assert len(promoted_trials) == len(results)


def test_generate_and_execute_prompt_perturbation(tmp_path: Path) -> None:
    arena = SelfPlayArena(tmp_path)
    prompt_store = PromptLayerStore(tmp_path)
    specs = arena.generate_prompt_perturbations(limit=2)
    assert len(specs) > 0
    spec = specs[0]
    assert spec.target_type == "prompt_layer"
    assert spec.candidate_content != ""
    assert "Strictly adhere" in spec.candidate_content

    result = arena.execute_shadow_trial(spec, auto_promote=True)
    assert result.passed
    assert result.promoted
    assert result.score_delta > 0.0

    overridden = prompt_store.read(spec.target_id)
    assert "Strictly adhere" in overridden


def test_generate_prompt_perturbation_domain_specialization(tmp_path: Path) -> None:
    from navi.credit_assignment import CreditAssignmentEngine

    credit_engine = CreditAssignmentEngine(tmp_path)
    credit_engine.record_attribution(
        trace_id="tr_checker",
        node_type="prompt_layer",
        target_id="instructions",
        outcome="failure",
        failure_domain="checker_blocked",
        reward=-0.6,
        delta_applied=-0.6,
        reason="test_blocked",
    )

    arena = SelfPlayArena(tmp_path)
    specs = arena.generate_prompt_perturbations(limit=1)
    assert len(specs) == 1
    assert specs[0].hypothesis == "append_grounded_evidence_refinement"
    assert "verified against grounded context" in specs[0].candidate_content

