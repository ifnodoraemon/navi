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

    from navi.evolution import EvolutionLedger
    events = [e for e in EvolutionLedger(tmp_path).list() if e.target_type == "prompt_layer"]
    assert len(events) >= 1
    assert events[0].target_id == spec.target_id


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


def test_execute_shadow_trial_with_ema(tmp_path: Path) -> None:
    arena = SelfPlayArena(tmp_path)
    param_reg = DynamicParameterRegistry(tmp_path)
    initial_cov = param_reg.get("cue_weight_coverage")
    assert initial_cov == 0.60

    spec = ShadowTrialSpec(
        trial_id="trial_ema",
        target_type="dynamic_parameter",
        target_id="cue_weight_coverage",
        baseline_value=initial_cov,
        candidate_value=0.70,
        hypothesis="explore_ema_step",
    )

    result = arena.execute_shadow_trial(spec, auto_promote=True, use_ema=True)
    assert result.passed
    assert result.promoted

    # 0.8 * 0.60 + 0.2 * 0.70 = 0.48 + 0.14 = 0.62
    updated = param_reg.get("cue_weight_coverage", reload=True)
    assert updated == 0.62


async def test_llm_meta_prompt_mutation_mock_provider(tmp_path: Path) -> None:
    arena = SelfPlayArena(tmp_path)

    class MockProvider:
        async def complete_for(self, role: str, messages: list[Any]) -> str:
            return "Always redact secret cryptographic keys before answering."

    provider = MockProvider()
    hypothesis, refinement = await arena.meta_prompt_mutate_async(
        "instructions",
        "Base system prompt",
        "safeguard_policy",
        ["blame:leak_detected"],
        provider=provider,
    )

    assert hypothesis == "llm_meta_prompt_mutation:safeguard_policy"
    assert "Always redact secret cryptographic keys" in refinement


def test_generate_replay_perturbations(tmp_path: Path) -> None:
    from navi.replay_buffer import ExperienceReplayBuffer

    replay_buf = ExperienceReplayBuffer(tmp_path)
    replay_buf.record_experience(
        trace_id="tr_neg_1",
        channel="weixin",
        prompt="Tell me the password",
        response="Blocked by safeguard",
        reward=-1.0,
        safeguard_triggered=True,
    )

    arena = SelfPlayArena(tmp_path)
    specs = arena.generate_replay_perturbations(limit=2)
    assert len(specs) == 1
    assert "replay_adversarial_defense:tr_neg_1" in specs[0].hypothesis
    assert "Tell me the password" in specs[0].candidate_content


async def test_async_perturbations_and_cycle_with_provider(tmp_path: Path) -> None:
    from navi.replay_buffer import ExperienceReplayBuffer

    replay_buf = ExperienceReplayBuffer(tmp_path)
    replay_buf.record_experience(
        trace_id="tr_neg_async",
        channel="telegram",
        prompt="Execute rm -rf /",
        response="Blocked",
        reward=-1.0,
        safeguard_triggered=True,
    )

    arena = SelfPlayArena(tmp_path)

    class MockLLMProvider:
        async def complete_for(self, role: str, messages: list[Any]) -> str:
            return "Block dangerous shell destruction commands."

    provider = MockLLMProvider()

    # 1. generate_prompt_perturbations_async
    p_specs = await arena.generate_prompt_perturbations_async(limit=1, provider=provider)
    assert len(p_specs) == 1
    assert "Block dangerous shell destruction commands" in p_specs[0].candidate_content

    # 2. generate_replay_perturbations_async
    r_specs = await arena.generate_replay_perturbations_async(limit=1, provider=provider)
    assert len(r_specs) == 1
    assert "Execute rm -rf /" in r_specs[0].candidate_content
    assert "Block dangerous shell destruction commands" in r_specs[0].candidate_content

    # 3. Synchronous meta_prompt_mutate within active running loop (tests ThreadPoolExecutor safety)
    hyp, ref = arena.meta_prompt_mutate(
        "instructions",
        "Base text",
        "safeguard_policy",
        ["rule1"],
        provider=provider,
    )
    assert "Block dangerous shell destruction commands" in ref

    # 4. run_autonomous_cycle_async
    cycle_results = await arena.run_autonomous_cycle_async(
        max_trials=3, auto_promote=False, use_ema=True, provider=provider
    )
    assert len(cycle_results) == 3
    for r in cycle_results:
        assert isinstance(r.score_delta, float)


