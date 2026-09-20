"""Tests for Self-Play Shadow Arena and autonomous parameter exploration."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from navi.dynamic_parameters import DynamicParameterRegistry
from navi.prompting import PromptLayerStore
from navi.self_play import (
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


def _write_behavioral_case(home: Path, case_id: str = "test_behavioral_case") -> str:
    case_path = home / "evals" / f"{case_id}.json"
    case_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.write_text(
        json.dumps(
            {
                "id": case_id,
                "target_types": ["dynamic_parameter", "prompt_layer"],
                "assertions": [{"type": "nonempty"}],
            }
        )
    )
    return case_id


def test_execute_shadow_trial_stub_verification_blocks_promotion(tmp_path: Path) -> None:
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
    # Runtime stubs ("runtime.parameter.valid") prove only that the value
    # parses; they must not gate live-state mutation on their own.
    assert not result.promoted
    assert result.score_delta == 0.0
    assert (
        result.evidence["promotion_blocked_reason"]
        == "structural_stub_verification_only"
    )

    # Registry unchanged
    assert param_reg.get("cue_weight_coverage", reload=True) == initial_cov

    # Trial record persisted with the block reason
    trials = arena.list_trials(target_id="cue_weight_coverage")
    assert len(trials) == 1
    assert not trials[0].promoted
    assert (
        trials[0].evidence["promotion_blocked_reason"]
        == "structural_stub_verification_only"
    )


def test_execute_shadow_trial_promotion_with_behavioral_eval_case(tmp_path: Path) -> None:
    case_id = _write_behavioral_case(tmp_path)
    arena = SelfPlayArena(tmp_path)
    param_reg = DynamicParameterRegistry(tmp_path)
    initial_cov = param_reg.get("cue_weight_coverage")

    spec = ShadowTrialSpec(
        trial_id="trial_behavioral",
        target_type="dynamic_parameter",
        target_id="cue_weight_coverage",
        baseline_value=initial_cov,
        candidate_value=0.65,
        hypothesis="explore_higher_coverage_verified",
        eval_case_ids=(case_id,),
    )

    result = arena.execute_shadow_trial(spec, auto_promote=True)
    assert result.passed
    assert result.promoted
    assert result.score_delta > 0.0
    assert param_reg.get("cue_weight_coverage", reload=True) == 0.65


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


def _assert_gated_promotion_result(res) -> None:
    assert res.promoted
    assert res.evidence["memory_gate"]["blocked"] is False
    assert res.score_delta > 0.0


def _assert_stub_observation_result(res) -> None:
    assert not res.promoted
    assert res.score_delta == 0.0


def test_run_autonomous_cycle(tmp_path: Path) -> None:
    arena = SelfPlayArena(tmp_path)
    results = arena.run_autonomous_cycle(max_trials=3, auto_promote=True)
    assert len(results) > 0
    assert len(results) <= 3
    for res in results:
        assert res.passed
        # Memory-plane parameters carry the behavioral regression gate and
        # may promote; everything else runs as a structural-stub shadow
        # observation and must never mutate live state.
        has_gate = bool(res.evidence.get("memory_gate"))
        result_checks = {
            True: _assert_gated_promotion_result,
            False: _assert_stub_observation_result,
        }
        result_checks[has_gate](res)

    promoted_trials = arena.list_trials(promoted_only=True)
    gated_promotions = [
        res for res in results if res.evidence.get("memory_gate")
    ]
    assert len(promoted_trials) == len(gated_promotions)


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
    # Stub verification ("runtime.text.nonempty") must never rewrite the live
    # prompt layer.
    assert not result.promoted
    assert (
        result.evidence["promotion_blocked_reason"]
        == "structural_stub_verification_only"
    )

    # The live layer is unchanged (still the default spec content).
    overridden = prompt_store.read(spec.target_id)
    assert "Strictly adhere" not in overridden
    assert "Always produce structured tool calls" in overridden

    from navi.evolution import EvolutionLedger

    events = [e for e in EvolutionLedger(tmp_path).list() if e.target_type == "prompt_layer"]
    assert len(events) == 0


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
    case_id = _write_behavioral_case(tmp_path)
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
        eval_case_ids=(case_id,),
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
    # The replay entry is untrusted conversation history: the candidate must
    # be derived from the current live instructions layer, never from the
    # stored user message.
    assert "Tell me the password" not in specs[0].candidate_content
    assert "Always produce structured tool calls" in specs[0].candidate_content


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
    # Candidates derive from the live instructions layer, not the replayed
    # user message.
    assert "Execute rm -rf /" not in r_specs[0].candidate_content
    assert "Always produce structured tool calls" in r_specs[0].candidate_content
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


def test_claim_cycle_if_due_enforces_interval(tmp_path: Path) -> None:
    arena = SelfPlayArena(tmp_path)
    assert arena.last_cycle_at() == 0.0

    # First claim succeeds once the full interval has elapsed since epoch 0.
    assert arena.claim_cycle_if_due(3600.0, now=3600.0) is True
    assert arena.last_cycle_at() == 3600.0

    # Within the interval: throttled.
    assert arena.claim_cycle_if_due(3600.0, now=5000.0) is False
    assert arena.last_cycle_at() == 3600.0

    # Past the interval: claim succeeds again.
    assert arena.claim_cycle_if_due(3600.0, now=7200.0) is True
    assert arena.last_cycle_at() == 7200.0


def test_prompt_perturbation_dedupe_skips_existing_directive(tmp_path: Path) -> None:
    arena = SelfPlayArena(tmp_path)
    prompt_store = PromptLayerStore(tmp_path)
    # The heuristic fallback for unknown domains appends the schema-constraint
    # directive; once it is already part of the live layer it must not be
    # appended again.
    prompt_store.write_override(
        "instructions",
        "Base instructions.\nStrictly adhere to schema formats and output constraints.",
    )

    specs = arena.generate_prompt_perturbations(limit=1)
    assert all(spec.target_id != "instructions" for spec in specs)


def test_prompt_perturbation_growth_cap_blocks_promotion(tmp_path: Path) -> None:
    case_id = _write_behavioral_case(tmp_path)
    arena = SelfPlayArena(tmp_path)
    prompt_store = PromptLayerStore(tmp_path)
    current = prompt_store.read("instructions")

    spec = ShadowTrialSpec(
        trial_id="trial_huge",
        target_type="prompt_layer",
        target_id="instructions",
        baseline_value=float(len(current)),
        candidate_value=float(len(current) + 20000),
        hypothesis="oversized_candidate",
        eval_case_ids=(case_id,),
        candidate_content="A" * (len(current) + 20000),
    )

    result = arena.execute_shadow_trial(spec, auto_promote=True)
    assert result.passed
    assert not result.promoted
    assert result.evidence["promotion_blocked_reason"] == "candidate_exceeds_growth_cap"
    assert prompt_store.read("instructions") == current


def test_evidence_uses_digest_not_full_content(tmp_path: Path) -> None:
    arena = SelfPlayArena(tmp_path)
    prompt_store = PromptLayerStore(tmp_path)
    current = prompt_store.read("instructions")

    spec = ShadowTrialSpec(
        trial_id="trial_digest",
        target_type="prompt_layer",
        target_id="instructions",
        baseline_value=float(len(current)),
        candidate_value=float(len(current) + 10),
        hypothesis="digest_check",
        eval_case_ids=("runtime.text.nonempty",),
        candidate_content=current + "0123456789",
    )

    result = arena.execute_shadow_trial(spec, auto_promote=False)
    assert "candidate_content" not in result.evidence
    digest = result.evidence["candidate_digest"]
    assert digest["length"] == len(current) + 10
    assert len(digest["sha256"]) == 64
    assert digest["head"] == (current + "0123456789")[:200]




def test_execute_shadow_trial_memory_gate_blocks_contamination(tmp_path: Path) -> None:
    arena = SelfPlayArena(tmp_path)
    param_reg = DynamicParameterRegistry(tmp_path)
    initial = param_reg.get("tf_max_extra_boost")

    spec = ShadowTrialSpec(
        trial_id="trial_memory_gate_block",
        target_type="dynamic_parameter",
        target_id="tf_max_extra_boost",
        baseline_value=initial,
        candidate_value=9643190.0,
        hypothesis="explore_huge_tf_boost",
        eval_case_ids=("memory.regression.gate", "runtime.parameter.valid"),
    )

    result = arena.execute_shadow_trial(spec, auto_promote=True)

    assert result.passed
    assert not result.promoted
    assert result.evidence["promotion_blocked_reason"] == "memory_regression_gate_failed"
    assert result.evidence["memory_gate"]["blocked"] is True
    assert param_reg.get("tf_max_extra_boost", reload=True) == initial


def test_execute_shadow_trial_memory_gate_allows_benign_candidate(tmp_path: Path) -> None:
    arena = SelfPlayArena(tmp_path)
    param_reg = DynamicParameterRegistry(tmp_path)
    initial = param_reg.get("cue_weight_coverage")

    spec = ShadowTrialSpec(
        trial_id="trial_memory_gate_pass",
        target_type="dynamic_parameter",
        target_id="cue_weight_coverage",
        baseline_value=initial,
        candidate_value=0.65,
        hypothesis="explore_higher_coverage_gated",
        eval_case_ids=("memory.regression.gate", "runtime.parameter.valid"),
    )

    result = arena.execute_shadow_trial(spec, auto_promote=True)

    assert result.passed
    assert result.promoted
    assert result.evidence["memory_gate"]["blocked"] is False
    assert param_reg.get("cue_weight_coverage", reload=True) == 0.65


def test_generate_parameter_perturbations_marks_memory_params_behavioral(tmp_path: Path) -> None:
    from navi.memory_eval import MEMORY_PARAMETER_SET

    arena = SelfPlayArena(tmp_path)
    specs = arena.generate_parameter_perturbations(limit=60)
    assert specs
    memory_specs = [spec for spec in specs if spec.target_id in MEMORY_PARAMETER_SET]
    assert memory_specs
    for spec in memory_specs:
        assert "memory.regression.gate" in spec.eval_case_ids
    for spec in specs:
        if spec.target_id not in MEMORY_PARAMETER_SET:
            assert spec.eval_case_ids == ("runtime.parameter.valid",)
