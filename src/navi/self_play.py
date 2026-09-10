"""Self-Play Shadow Arena: autonomous reinforcement exploration and verification.

Enables test-time compute and self-play for the Agentic Neural Network:
1. Generates adversarial perturbations and parameter hypotheses.
2. Runs shadow trials in isolated verification harness.
3. Promotes strictly verified candidates back to dynamic parameter matrix.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any
import uuid

from .db import connect
from .dynamic_parameters import DynamicParameterRegistry, SYSTEM_DYNAMIC_PARAMETERS
from .credit_assignment import CreditAssignmentEngine
from .evolution_experiments import EvolutionExperimentStore
from .paths import db_paths
from .prompting import PromptLayerStore
from .schema import Column, Table, assert_schema_exact


SELF_PLAY_TRIALS_TABLE = Table(
    "self_play_trials",
    [
        Column("trial_id", "TEXT", primary_key=True),
        Column("target_type", "TEXT", nullable=False),
        Column("target_id", "TEXT", nullable=False),
        Column("baseline_value", "REAL", nullable=False),
        Column("candidate_value", "REAL", nullable=False),
        Column("hypothesis", "TEXT", nullable=False),
        Column("score_delta", "REAL", nullable=False),
        Column("passed", "INTEGER", nullable=False),
        Column("promoted", "INTEGER", nullable=False),
        Column("evidence_json", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
    ],
)


@dataclass(frozen=True)
class ShadowTrialSpec:
    trial_id: str
    target_type: str
    target_id: str
    baseline_value: float
    candidate_value: float
    hypothesis: str
    eval_case_ids: tuple[str, ...] = ("runtime.parameter.valid",)
    candidate_content: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "baseline_value": self.baseline_value,
            "candidate_value": self.candidate_value,
            "hypothesis": self.hypothesis,
            "eval_case_ids": list(self.eval_case_ids),
            "candidate_content": self.candidate_content,
        }


@dataclass(frozen=True)
class ShadowTrialResult:
    trial_id: str
    target_type: str
    target_id: str
    baseline_value: float
    candidate_value: float
    score_delta: float
    passed: bool
    promoted: bool
    evidence: dict[str, Any]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "baseline_value": self.baseline_value,
            "candidate_value": self.candidate_value,
            "score_delta": self.score_delta,
            "passed": self.passed,
            "promoted": self.promoted,
            "evidence": dict(self.evidence),
            "created_at": self.created_at,
        }


_PARAMETER_EXPLORATION_BOUNDS: dict[str, tuple[float, float, float]] = {
    # target_id -> (min_val, max_val, default_step)
    "cue_weight_coverage": (0.10, 0.90, 0.05),
    "cue_weight_jaccard": (0.05, 0.80, 0.05),
    "cue_weight_sequence": (0.05, 0.80, 0.05),
    "decay_base_delta": (0.01, 0.20, 0.02),
    "decay_stale_threshold": (0.05, 0.50, 0.05),
    "ltp_boost_delta": (0.01, 0.20, 0.02),
    "provider_retry_after_seconds": (5.0, 60.0, 2.5),
    "loop_max_turns": (10.0, 60.0, 5.0),
    "loop_max_attempts_turn": (2.0, 10.0, 1.0),
    "loop_max_attempts_control": (1.0, 6.0, 1.0),
    "loop_max_attempts_scheduled": (3.0, 12.0, 1.0),
    "loop_max_attempts_durable_goal": (5.0, 20.0, 1.0),
    "temporal_discount_factor": (0.50, 0.99, 0.05),
    "confidence_reduction_delta": (0.02, 0.30, 0.02),
    "prompt_mutation_temperature": (0.20, 1.00, 0.10),
    "prompt_evaluation_threshold": (0.50, 0.95, 0.05),
    "safeguards_entropy_threshold": (2.0, 8.0, 0.5),
    "safeguards_confidence_threshold": (0.50, 0.95, 0.05),
    "hebbian_learning_rate": (0.01, 0.20, 0.02),
    "credit_assignment_learning_rate": (0.01, 0.20, 0.02),
    "consolidation_default_confidence": (0.40, 0.90, 0.05),
    "graph_edge_prune_threshold": (0.05, 0.40, 0.05),
    "saga_lease_timeout_turn": (30.0, 300.0, 30.0),
    "saga_lease_timeout_control": (15.0, 120.0, 15.0),
    "daemon_poll_interval_seconds": (10.0, 300.0, 10.0),
    "transient_retention_seconds": (3600.0, 604800.0, 3600.0),
    "planner_fact_max_depth": (2.0, 16.0, 1.0),
    "model_fact_max_chars": (10000.0, 128000.0, 4000.0),
    "model_fact_max_string_chars": (1000.0, 10000.0, 500.0),
    "model_fact_max_depth": (2.0, 10.0, 1.0),
    "model_fact_max_items": (10.0, 100.0, 5.0),
    "context_evidence_max_items": (5.0, 50.0, 5.0),
    "context_evidence_excerpt_chars": (200.0, 2000.0, 100.0),
    "context_recent_message_limit": (2.0, 20.0, 2.0),
    "outbox_max_attempts": (1.0, 10.0, 1.0),
    "outbox_stale_sending_seconds": (60.0, 1800.0, 60.0),
    "child_max_active": (1.0, 10.0, 1.0),
    "child_max_timeout_seconds": (60.0, 3600.0, 60.0),
    "child_max_token_budget": (10000.0, 200000.0, 10000.0),
    "child_max_call_budget": (3.0, 50.0, 3.0),
    "child_max_cost_budget": (0.5, 10.0, 0.5),
    "child_max_qps": (1.0, 20.0, 1.0),
    "reward_success": (0.5, 2.0, 0.1),
    "reward_degraded": (0.0, 0.5, 0.05),
    "severity_safeguard_policy": (0.5, 1.0, 0.1),
    "severity_loop_no_progress": (0.4, 1.0, 0.1),
    "severity_planner_or_parser": (0.3, 1.0, 0.1),
    "severity_checker_blocked": (0.3, 1.0, 0.1),
    "severity_capability_failure": (0.2, 0.9, 0.1),
    "severity_runtime": (0.2, 0.8, 0.1),
    "severity_provider_no_response": (0.1, 0.6, 0.1),
    "planner_context_message_limit": (50.0, 500.0, 25.0),
    "planner_context_recent_messages": (4.0, 30.0, 2.0),
    "planner_context_max_chars": (4000.0, 32000.0, 2000.0),
    "planner_context_older_preview_messages": (2.0, 20.0, 2.0),
    "planner_context_older_preview_chars": (100.0, 500.0, 20.0),
    "planner_context_recent_message_max_chars": (500.0, 5000.0, 250.0),
    "planner_memory_item_max_chars": (200.0, 2000.0, 100.0),
    "planner_attempt_history_limit": (2.0, 20.0, 2.0),
    "planner_attempt_history_max_chars": (4000.0, 32000.0, 2000.0),
    "planner_attempt_message_max_chars": (200.0, 2500.0, 100.0),
    "planner_prior_result_max_chars": (1000.0, 10000.0, 500.0),
    "planner_ambient_record_limit": (1.0, 10.0, 1.0),
    "semantic_checker_attempt_limit": (1.0, 10.0, 1.0),
    "semantic_checker_args_max_chars": (500.0, 8000.0, 500.0),
    "semantic_checker_facts_max_chars": (1000.0, 15000.0, 1000.0),
    "semantic_checker_message_max_chars": (500.0, 8000.0, 500.0),
    "semantic_checker_evidence_summary_max_chars": (500.0, 5000.0, 250.0),
    "semantic_checker_verdict_error_chars": (50.0, 500.0, 25.0),
    "semantic_checker_verdict_retries": (0.0, 3.0, 1.0),
    "task_result_preview_chars": (60.0, 600.0, 30.0),
    "provider_transport_max_retries": (1.0, 8.0, 1.0),
    "provider_transport_retry_min_seconds": (0.5, 5.0, 0.5),
    "provider_transport_retry_max_seconds": (60.0, 600.0, 30.0),
    "execution_lease_min_seconds": (300.0, 3600.0, 150.0),
    "execution_lease_heartbeat_max_seconds": (10.0, 90.0, 5.0),
    "default_port_probe_timeout_seconds": (0.2, 5.0, 0.2),
    "daemon_project_event_concurrency": (1.0, 16.0, 1.0),
    "max_git_status_prompt_chars": (1000.0, 20000.0, 1000.0),
    "max_log_read_bytes": (64000.0, 2000000.0, 64000.0),
    "max_log_prompt_chars": (20000.0, 300000.0, 20000.0),
    "provider_error_max_chars": (200.0, 3000.0, 100.0),
    "skill_file_max_bytes": (50000.0, 1000000.0, 50000.0),
    "search_title_max_chars": (100.0, 800.0, 50.0),
    "search_snippet_max_chars": (300.0, 3000.0, 150.0),
    "search_response_max_bytes": (500000.0, 8000000.0, 500000.0),
    "search_x_response_max_bytes": (1000000.0, 16000000.0, 1000000.0),
    "connector_idle_timeout_seconds": (30.0, 600.0, 30.0),
    "connector_heartbeat_interval_seconds": (5.0, 60.0, 5.0),
    "telegram_get_file_timeout_seconds": (5.0, 60.0, 5.0),
    "telegram_download_timeout_seconds": (15.0, 300.0, 15.0),
    "weixin_config_timeout_seconds": (2.0, 30.0, 2.0),
    "weixin_context_token_max_age_seconds": (3600.0, 259200.0, 3600.0),
    "weixin_ingress_stale_after_seconds": (30.0, 600.0, 30.0),
}


def _get_parameter_bounds(param_name: str, current_value: float) -> tuple[float, float, float]:
    entry = _PARAMETER_EXPLORATION_BOUNDS.get(param_name)
    if entry is not None:
        return entry
    step = round(max(0.01, abs(current_value) * 0.1), 4)
    min_val = round(min(current_value - step, current_value * 0.5), 4)
    max_val = round(max(current_value + step, current_value * 2.0), 4)
    return (min_val, max_val, step)


class SelfPlayArena:
    """Self-play exploration engine executing shadow trials to improve dynamic parameters."""

    def __init__(self, home: Path):
        self.home = home
        self.db_path = db_paths(home).evolution
        self.param_registry = DynamicParameterRegistry(home)
        self.experiment_store = EvolutionExperimentStore(home)
        self.credit_engine = CreditAssignmentEngine(home)
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.execute(SELF_PLAY_TRIALS_TABLE.ddl)
            assert_schema_exact(conn, SELF_PLAY_TRIALS_TABLE)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_self_play_target "
                "ON self_play_trials(target_type, target_id)"
            )

    def generate_parameter_perturbations(self, limit: int = 5) -> list[ShadowTrialSpec]:
        """Generate targeted parameter perturbations informed by credit attributions."""
        attributions = self.credit_engine.list_attributions(limit=50)
        param_attribution_counts: dict[str, int] = {}
        all_dynamic_params = set(SYSTEM_DYNAMIC_PARAMETERS.keys()) | set(_PARAMETER_EXPLORATION_BOUNDS.keys())
        for attr in attributions:
            name = attr.target_id
            if attr.node_type == "dynamic_parameter" and name in all_dynamic_params:
                param_attribution_counts[name] = param_attribution_counts.get(name, 0) + 1

        ordered_targets: list[str] = sorted(
            all_dynamic_params,
            key=lambda k: param_attribution_counts.get(k, 0),
            reverse=True,
        )

        specs: list[ShadowTrialSpec] = []
        for target_id in ordered_targets:
            if len(specs) >= limit:
                break
            current_val = self.param_registry.get(target_id)
            min_val, max_val, step = _get_parameter_bounds(target_id, current_val)
            # Explore upward step
            candidate_up = round(min(max_val, current_val + step), 4)
            if candidate_up != current_val:
                specs.append(
                    ShadowTrialSpec(
                        trial_id=uuid.uuid4().hex,
                        target_type="dynamic_parameter",
                        target_id=target_id,
                        baseline_value=current_val,
                        candidate_value=candidate_up,
                        hypothesis=f"exploratory_upward_step_by_{step}",
                    )
                )
            if len(specs) >= limit:
                break
            # Explore downward step
            candidate_down = round(max(min_val, current_val - step), 4)
            if candidate_down != current_val:
                specs.append(
                    ShadowTrialSpec(
                        trial_id=uuid.uuid4().hex,
                        target_type="dynamic_parameter",
                        target_id=target_id,
                        baseline_value=current_val,
                        candidate_value=candidate_down,
                        hypothesis=f"exploratory_downward_step_by_{step}",
                    )
                )

        return specs[:limit]

    async def meta_prompt_mutate_async(
        self,
        layer_id: str,
        current_content: str,
        failure_domain: str,
        failure_reasons: list[str],
        *,
        provider: Any | None = None,
    ) -> tuple[str, str]:
        """Compute meta-prompt gradient mutation using TextGrad / DSPy prompt loss reflection."""
        reasons_text = "\n".join(f"- {r}" for r in failure_reasons[:5])
        if not reasons_text:
            reasons_text = f"- {failure_domain}"

        if provider is not None and hasattr(provider, "complete_for"):
            try:
                from .provider import ChatMessage

                messages = [
                    ChatMessage(
                        role="system",
                        content=(
                            "Autonomous meta-prompt optimizer for an AI agent. "
                            "Given the failure domain and execution penalties, output a single, highly specific "
                            "defensive directive to append to the system prompt to prevent this failure in future runs. "
                            "Output ONLY the directive sentence, nothing else."
                        ),
                    ),
                    ChatMessage(
                        role="user",
                        content=(
                            f"Prompt Layer: {layer_id}\n"
                            f"Failure Domain: {failure_domain}\n"
                            f"Failure Attributions:\n{reasons_text}\n\n"
                            "Generate the defensive refinement directive:"
                        ),
                    ),
                ]
                response = await provider.complete_for("default", messages)
                cleaned = str(response).strip().strip('"').strip("'")
                if cleaned:
                    hypothesis = f"llm_meta_prompt_mutation:{failure_domain}"
                    refinement = f"\n{cleaned}\n"
                    return (hypothesis, refinement)
            except Exception:
                pass

        return self._heuristic_domain_mutation(failure_domain)

    def meta_prompt_mutate(
        self,
        layer_id: str,
        current_content: str,
        failure_domain: str,
        failure_reasons: list[str],
        *,
        provider: Any | None = None,
    ) -> tuple[str, str]:
        """Synchronous wrapper for meta_prompt_mutate_async or heuristic fallback."""
        if provider is not None and hasattr(provider, "complete_for"):
            try:
                import asyncio
                return asyncio.run(
                    self.meta_prompt_mutate_async(
                        layer_id,
                        current_content,
                        failure_domain,
                        failure_reasons,
                        provider=provider,
                    )
                )
            except Exception:
                pass
        return self._heuristic_domain_mutation(failure_domain)

    @staticmethod
    def _heuristic_domain_mutation(failure_domain: str) -> tuple[str, str]:
        domain_mutations: dict[str, tuple[str, str]] = {
            "planner_or_parser": (
                "append_schema_constraint_refinement",
                "\nStrictly adhere to schema formats and output constraints.",
            ),
            "checker_blocked": (
                "append_grounded_evidence_refinement",
                "\nEnsure all claims are explicitly verified against grounded context.",
            ),
            "loop_no_progress": (
                "append_progress_convergence_refinement",
                "\nAdvance execution systematically towards completion without circular retries.",
            ),
            "safeguard_policy": (
                "append_safeguards_compliance_refinement",
                "\nRedact and protect sensitive information and cryptographic credentials.",
            ),
            "capability_failure": (
                "append_capability_resilience_refinement",
                "\nVerify tool prerequisites and handle execution errors gracefully.",
            ),
            "runtime": (
                "append_runtime_stability_refinement",
                "\nEnforce strict execution bounds and resilient state transitions.",
            ),
            "missing_completion_check": (
                "append_terminal_verification_refinement",
                "\nExplicitly verify terminal criteria before marking execution complete.",
            ),
        }
        res = domain_mutations.get(
            failure_domain,
            (
                "append_schema_constraint_refinement",
                "\nStrictly adhere to schema formats and output constraints.",
            ),
        )
        return res

    def generate_prompt_perturbations(
        self,
        limit: int = 2,
        *,
        provider: Any | None = None,
    ) -> list[ShadowTrialSpec]:
        """Generate targeted prompt layer perturbations informed by blame credit attributions."""
        prompt_store = PromptLayerStore(self.home)
        attributions = self.credit_engine.list_attributions(limit=50)
        blamed_prompt_layers: dict[str, int] = {}
        blamed_domains: dict[str, str] = {}
        blamed_reasons: dict[str, list[str]] = {}

        for attr in attributions:
            name = attr.target_id
            if attr.node_type == "prompt_layer" and prompt_store.is_declared(name):
                blamed_prompt_layers[name] = blamed_prompt_layers.get(name, 0) + 1
                if name not in blamed_domains:
                    blamed_domains[name] = attr.failure_domain
                if name not in blamed_reasons:
                    blamed_reasons[name] = []
                blamed_reasons[name].append(attr.reason)

        ordered_layers: list[str] = sorted(
            blamed_prompt_layers.keys(),
            key=lambda k: blamed_prompt_layers.get(k, 0),
            reverse=True,
        )
        if not ordered_layers:
            ordered_layers = [name for name in ("instructions", "identity") if prompt_store.is_declared(name)]

        specs: list[ShadowTrialSpec] = []
        for layer_id in ordered_layers:
            if len(specs) >= limit:
                break
            current_content = prompt_store.read(layer_id)
            domain = blamed_domains.get(layer_id, "default")
            reasons = blamed_reasons.get(layer_id, [])

            hypothesis, refinement = self.meta_prompt_mutate(
                layer_id,
                current_content,
                domain,
                reasons,
                provider=provider,
            )
            candidate_text = current_content.strip() + refinement + "\n"
            specs.append(
                ShadowTrialSpec(
                    trial_id=uuid.uuid4().hex,
                    target_type="prompt_layer",
                    target_id=layer_id,
                    baseline_value=float(len(current_content)),
                    candidate_value=float(len(candidate_text)),
                    hypothesis=hypothesis,
                    eval_case_ids=("runtime.text.nonempty",),
                    candidate_content=candidate_text,
                )
            )

        return specs[:limit]

    def generate_replay_perturbations(
        self,
        limit: int = 5,
        *,
        channels: list[str] | None = None,
        provider: Any | None = None,
    ) -> list[ShadowTrialSpec]:
        """Generate shadow trial specifications from replay buffer hard negatives."""
        from .replay_buffer import ExperienceReplayBuffer

        replay_buffer = ExperienceReplayBuffer(self.home)
        target_channel: str | None = None
        if channels is not None and len(channels) == 1:
            target_channel = channels[0]
        hard_negatives = replay_buffer.get_hard_negatives(limit=limit, channel=target_channel)
        specs: list[ShadowTrialSpec] = []
        for entry in hard_negatives:
            if len(specs) >= limit:
                break
            f_domain = str(entry.metadata.get("failure_domain", "safeguard_policy"))
            hypothesis, refinement = self.meta_prompt_mutate(
                "instructions",
                entry.prompt,
                f_domain,
                [f"replay_trace:{entry.trace_id}", f"reward:{entry.reward}"],
                provider=provider,
            )
            candidate_text = entry.prompt.strip() + "\n" + refinement.strip() + "\n"
            specs.append(
                ShadowTrialSpec(
                    trial_id=uuid.uuid4().hex,
                    target_type="prompt_layer",
                    target_id="instructions",
                    baseline_value=float(len(entry.prompt)),
                    candidate_value=float(len(candidate_text)),
                    hypothesis=f"replay_adversarial_defense:{entry.trace_id[:8]}",
                    eval_case_ids=("runtime.text.nonempty",),
                    candidate_content=candidate_text,
                )
            )
        return specs[:limit]

    def execute_shadow_trial(
        self,
        spec: ShadowTrialSpec,
        *,
        auto_promote: bool = True,
        use_ema: bool = False,
        now: float | None = None,
    ) -> ShadowTrialResult:
        """Run a shadow trial against verification checks and optionally promote."""
        current_time = time.time()
        if now is not None:
            current_time = float(now)

        candidate_payload = json.dumps(
            {
                "value": spec.candidate_value,
                "reason": f"shadow_trial:{spec.hypothesis}",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if spec.target_type == "prompt_layer":
            candidate_payload = spec.candidate_content

        all_checks: list[dict[str, Any]] = []
        for case_id in spec.eval_case_ids:
            checks = self.experiment_store._evaluate_case(
                case_id=case_id,
                target_type=spec.target_type,
                candidate=candidate_payload,
            )
            all_checks.extend(checks)

        passed = bool(all_checks) and all(bool(c.get("passed")) for c in all_checks)
        score_delta = 0.0
        if passed:
            passing_thresh = self.param_registry.get("evals_passing_score_threshold", 0.85)
            score_delta = round(passing_thresh * 0.1, 4)

        promoted = passed and auto_promote
        if promoted:
            if spec.target_type == "dynamic_parameter":
                if use_ema:
                    self.param_registry.apply_ema(
                        spec.target_id,
                        spec.candidate_value,
                        reason=f"self_play_promoted_ema:{spec.trial_id}",
                    )
                if not use_ema:
                    self.param_registry.set(
                        spec.target_id,
                        spec.candidate_value,
                        reason=f"self_play_promoted:{spec.trial_id}",
                    )
            if spec.target_type == "prompt_layer":
                prompt_store = PromptLayerStore(self.home)
                prompt_store.write_override(spec.target_id, spec.candidate_content)

        evidence = {
            "checks": all_checks,
            "hypothesis": spec.hypothesis,
            "target_type": spec.target_type,
            "target_id": spec.target_id,
            "candidate_content": spec.candidate_content,
        }

        result = ShadowTrialResult(
            trial_id=spec.trial_id,
            target_type=spec.target_type,
            target_id=spec.target_id,
            baseline_value=spec.baseline_value,
            candidate_value=spec.candidate_value,
            score_delta=score_delta,
            passed=passed,
            promoted=promoted,
            evidence=evidence,
            created_at=current_time,
        )

        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO self_play_trials (
                    trial_id, target_type, target_id, baseline_value,
                    candidate_value, hypothesis, score_delta, passed,
                    promoted, evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.trial_id,
                    result.target_type,
                    result.target_id,
                    result.baseline_value,
                    result.candidate_value,
                    spec.hypothesis,
                    result.score_delta,
                    int(result.passed),
                    int(result.promoted),
                    json.dumps(result.evidence, ensure_ascii=False, sort_keys=True),
                    result.created_at,
                ),
            )

        # Ingest synthetic self-play trial into multi-channel experience replay buffer
        try:
            from .replay_buffer import ExperienceReplayBuffer

            trial_reward = 1.0
            if not passed:
                trial_reward = -0.5
            resp = str(spec.candidate_value)
            if spec.candidate_content:
                resp = spec.candidate_content
            replay_buf = ExperienceReplayBuffer(self.home)
            replay_buf.record_experience(
                trace_id=f"self_play_{spec.trial_id[:8]}",
                channel="synthetic",
                prompt=spec.hypothesis,
                response=resp,
                reward=trial_reward,
                metadata={
                    "target_type": spec.target_type,
                    "target_id": spec.target_id,
                    "passed": passed,
                    "promoted": promoted,
                },
                now=current_time,
            )
        except Exception:
            pass

        return result

    def list_trials(
        self,
        *,
        target_id: str | None = None,
        promoted_only: bool = False,
        limit: int = 50,
    ) -> list[ShadowTrialResult]:
        predicates: list[str] = []
        params: list[Any] = []
        if target_id is not None:
            predicates.append("target_id = ?")
            params.append(target_id)
        if promoted_only:
            predicates.append("promoted = 1")
        where_clause = ""
        if predicates:
            where_clause = f"WHERE {' AND '.join(predicates)}"
        sql = f"""
            SELECT trial_id, target_type, target_id, baseline_value,
                   candidate_value, score_delta, passed, promoted,
                   evidence_json, created_at
            FROM self_play_trials
            {where_clause}
            ORDER BY created_at DESC
            LIMIT ?
        """
        params.append(limit)
        with connect(self.db_path) as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()

        return [
            ShadowTrialResult(
                trial_id=row[0],
                target_type=row[1],
                target_id=row[2],
                baseline_value=float(row[3]),
                candidate_value=float(row[4]),
                score_delta=float(row[5]),
                passed=bool(row[6]),
                promoted=bool(row[7]),
                evidence=json.loads(row[8]),
                created_at=float(row[9]),
            )
            for row in rows
        ]

    def run_autonomous_cycle(
        self,
        max_trials: int = 3,
        *,
        auto_promote: bool = True,
        use_ema: bool = False,
    ) -> list[ShadowTrialResult]:
        """Execute a full autonomous exploration and verification cycle across parameters, prompts, and replay buffer."""
        param_limit = max(1, max_trials - 2)
        specs: list[ShadowTrialSpec] = []
        specs.extend(self.generate_parameter_perturbations(limit=param_limit))
        specs.extend(self.generate_prompt_perturbations(limit=1))
        specs.extend(self.generate_replay_perturbations(limit=1))

        results: list[ShadowTrialResult] = []
        for spec in specs[:max_trials]:
            res = self.execute_shadow_trial(spec, auto_promote=auto_promote, use_ema=use_ema)
            results.append(res)
        return results
