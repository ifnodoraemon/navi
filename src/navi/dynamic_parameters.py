"""System-wide dynamic parameter store and runtime registry.

Enables online plasticity for neural-style agent adaptation:
All thresholds, decay rates, retry backoffs, and score weights are dynamic parameters
rather than immutable code constants.
"""
from __future__ import annotations

from pathlib import Path
import time
from typing import TYPE_CHECKING, Any

from .paths import db_paths

if TYPE_CHECKING:
    from .memory.provider import SQLiteMemoryProvider

SYSTEM_DYNAMIC_PARAMETERS: dict[str, float] = {
    # Cognitive and Memory weights
    "cue_weight_coverage": 0.60,
    "cue_weight_jaccard": 0.25,
    "cue_weight_sequence": 0.15,
    "tf_reinforcement_rate": 0.15,
    "tf_max_extra_boost": 3.0,
    "ltp_boost_delta": 0.05,
    "decay_base_delta": 0.05,
    "decay_stale_threshold": 0.20,
    "decay_grace_seconds": 90.0 * 86400.0,
    "decay_stability_scale": 1.0,
    "graph_fanout_damping": 3.0,
    "hebbian_learning_rate": 0.05,
    "credit_assignment_learning_rate": 0.05,
    "temporal_discount_factor": 0.85,
    "confidence_reduction_delta": 0.10,
    "consolidation_default_confidence": 0.70,
    "consolidation_history_retention_seconds": 90.0 * 86400.0,
    "graph_hub_degree_cutoff_multiplier": 3.0,
    "recall_fts_pool_multiplier": 3.0,
    "recall_lexical_pool_multiplier": 20.0,
    "recall_candidate_pool_min": 200.0,
    "consolidation_lease_seconds": 300.0,
    "context_recent_base_no_query": 950.0,
    "context_recent_base_query": 620.0,
    "context_message_rank_base_score": 900.0,
    "context_memory_recall_base_score": 800.0,

    # Transport and network retry parameters
    "provider_retry_after_seconds": 15.0,
    "weixin_rate_limit_retry_seconds": 900.0,
    "weixin_transient_rejection_retry_seconds": 60.0,

    # Lifecycle and transaction parameters
    "saga_grace_seconds": 60.0,
    "detached_recovery_grace_seconds": 90.0,
    "effect_lease_seconds": 900.0,
    "watchdog_heartbeat_timeout_seconds": 120.0,

    # Safeguards and security thresholds
    "safeguards_entropy_threshold": 4.5,
    "safeguards_confidence_threshold": 0.80,

    # Execution limits and retry budgets
    "loop_max_turns": 30.0,
    "loop_max_attempts_turn": 5.0,
    "loop_max_attempts_control": 3.0,
    "loop_max_attempts_scheduled": 6.0,
    "loop_max_attempts_durable_goal": 10.0,
    "max_parallel_tool_calls": 5.0,
    "evals_passing_score_threshold": 0.85,

    # Daemon and retention windows
    "daemon_poll_interval_seconds": 60.0,
    "transient_retention_seconds": 86400.0,

    # Prompt OS facts and context projection limits
    "planner_fact_max_depth": 8.0,
    "model_fact_max_chars": 48000.0,
    "model_fact_max_string_chars": 4000.0,
    "model_fact_max_depth": 5.0,
    "model_fact_max_items": 30.0,
    "context_evidence_max_items": 20.0,
    "context_evidence_excerpt_chars": 700.0,
    "context_recent_message_limit": 6.0,

    # Delivery outbox parameters
    "outbox_max_attempts": 3.0,
    "outbox_stale_sending_seconds": 300.0,

    # Child agent execution bounds
    "child_max_active": 3.0,
    "child_max_timeout_seconds": 900.0,
    "child_max_token_budget": 50000.0,
    "child_max_call_budget": 12.0,
    "child_max_cost_budget": 2.0,
    "child_max_qps": 5.0,

    # Terminal reward landscape and domain failure severities
    "reward_success": 1.0,
    "reward_degraded": 0.2,
    "severity_safeguard_policy": 1.0,
    "severity_loop_no_progress": 0.8,
    "severity_planner_or_parser": 0.7,
    "severity_checker_blocked": 0.6,
    "severity_capability_failure": 0.5,
    "severity_runtime": 0.4,
    "severity_provider_no_response": 0.3,
    "severity_default": 0.5,

    # Self-play and prompt evolution parameters
    "prompt_mutation_temperature": 0.70,
    "prompt_evaluation_threshold": 0.80,

    # State graph context assembly and history parameters
    "planner_context_message_limit": 200.0,
    "planner_context_recent_messages": 12.0,
    "planner_context_max_chars": 12000.0,
    "planner_context_older_preview_messages": 8.0,
    "planner_context_older_preview_chars": 220.0,
    "planner_context_recent_message_max_chars": 2000.0,
    "planner_memory_item_max_chars": 800.0,
    "planner_attempt_history_limit": 8.0,
    "planner_attempt_history_max_chars": 16000.0,
    "planner_attempt_message_max_chars": 1000.0,
    "planner_prior_result_max_chars": 4000.0,
    "planner_ambient_record_limit": 3.0,

    # Semantic checker limits
    "semantic_checker_attempt_limit": 4.0,
    "semantic_checker_args_max_chars": 3000.0,
    "semantic_checker_facts_max_chars": 6000.0,
    "semantic_checker_message_max_chars": 3000.0,
    "semantic_checker_evidence_summary_max_chars": 2000.0,
    "semantic_checker_verdict_error_chars": 200.0,
    "semantic_checker_verdict_retries": 1.0,
    "task_result_preview_chars": 240.0,

    # Provider transport retries and execution leases
    "provider_transport_max_retries": 3.0,
    "provider_transport_retry_min_seconds": 1.0,
    "provider_transport_retry_max_seconds": 300.0,
    "execution_lease_min_seconds": 900.0,
    "execution_lease_heartbeat_max_seconds": 30.0,

    # Proactive daemon and detector limits
    "default_port_probe_timeout_seconds": 1.0,
    "daemon_project_event_concurrency": 4.0,
    "max_git_status_prompt_chars": 5000.0,
    "max_log_read_bytes": 512000.0,
    "max_log_prompt_chars": 100000.0,

    # Tool execution and payload limits
    "provider_error_max_chars": 1000.0,
    "skill_file_max_bytes": 200000.0,
    "search_title_max_chars": 300.0,
    "search_snippet_max_chars": 1200.0,
    "search_response_max_bytes": 2000000.0,
    "search_x_response_max_bytes": 4000000.0,

    # Connector runtime and channel timeouts
    "connector_idle_timeout_seconds": 120.0,
    "connector_heartbeat_interval_seconds": 20.0,
    "telegram_get_file_timeout_seconds": 15.0,
    "telegram_download_timeout_seconds": 60.0,
    "weixin_config_timeout_seconds": 10.0,
    "weixin_context_token_max_age_seconds": 86400.0,
    "weixin_ingress_stale_after_seconds": 180.0,

    # Optimization, Momentum and EMA parameters (Adam analog)
    "parameter_momentum_beta": 0.90,
    "parameter_learning_rate": 0.05,
    "parameter_ema_alpha": 0.80,

    # Experience Replay Buffer parameters
    "replay_buffer_capacity": 5000.0,
    "replay_priority_alpha": 0.60,
    "replay_priority_epsilon": 0.01,

    # Channel, networking and diagnostics timeouts
    "telegram_send_message_timeout_seconds": 30.0,
    "weixin_qr_timeout_seconds": 35.0,
    "weixin_updates_timeout_seconds": 40.0,
    "weixin_send_timeout_seconds": 15.0,
    "weixin_upload_timeout_seconds": 120.0,
    "diagnostics_probe_timeout_seconds": 5.0,
    "diagnostics_systemctl_timeout_seconds": 8.0,
    "git_command_timeout_seconds": 8.0,
    "git_detector_timeout_seconds": 10.0,
    "http_fetch_pinned_ip_timeout_seconds": 15.0,
    "event_bus_drain_timeout_seconds": 5.0,
    "event_bus_shutdown_timeout_seconds": 5.0,
}


class DynamicParameterRegistry:
    """Provides thread-safe, cached access to dynamic system parameters backed by SQLite."""

    def __init__(self, home: Path):
        from .memory.provider import SQLiteMemoryProvider

        self.home = home
        self.provider = SQLiteMemoryProvider(db_paths(home).memory)
        self._cache: dict[str, float] = {}
        self._last_loaded_at: float = 0.0
        self._cache_ttl_seconds: float = 5.0

    def _sync(self) -> None:
        now = time.time()
        if now - self._last_loaded_at < self._cache_ttl_seconds and self._cache:
            return
        for name, default_val in SYSTEM_DYNAMIC_PARAMETERS.items():
            self.provider.set_parameter_if_absent(
                name,
                default_val,
                updated_at=now,
                metadata={"reason": "system_default_initialization"},
            )
        persisted = self.provider.list_parameters()
        self._cache = {name: val for name, (val, _, _) in persisted.items()}
        self._last_loaded_at = now

    def invalidate_cache(self) -> None:
        self._cache.clear()
        self._last_loaded_at = 0.0

    def get(
        self,
        name: str,
        fallback: float | None = None,
        *,
        reload: bool = False,
    ) -> float:
        if reload:
            self.invalidate_cache()
        self._sync()
        default_val = SYSTEM_DYNAMIC_PARAMETERS.get(name, 0.0)
        effective_fallback = default_val
        if fallback is not None:
            effective_fallback = fallback
        return float(self._cache.get(name, effective_fallback))

    def set(
        self,
        name: str,
        value: float,
        *,
        reason: str = "dynamic_update",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        val = float(value)
        meta = dict(metadata or {})
        meta["reason"] = reason
        self._cache[name] = val
        self.provider.set_parameter(name, val, updated_at=now, metadata=meta)

    def apply_gradient(
        self,
        name: str,
        gradient: float,
        *,
        learning_rate: float | None = None,
        beta: float | None = None,
        bounds: tuple[float, float] | None = None,
        reason: str = "gradient_step",
    ) -> float:
        """Apply a smoothed Adam/momentum gradient step to a dynamic parameter."""
        current_val = self.get(name)
        all_params = self.list_all()
        meta = dict(all_params.get(name, {}).get("metadata", {}))

        lr = self.get("parameter_learning_rate", 0.05)
        if learning_rate is not None:
            lr = float(learning_rate)

        b = self.get("parameter_momentum_beta", 0.90)
        if beta is not None:
            b = float(beta)

        old_m = float(meta.get("momentum", 0.0))
        old_step = int(meta.get("step", 0))
        new_step = old_step + 1

        new_m = b * old_m + (1.0 - b) * gradient
        bias_correction = 1.0 - (b ** new_step)
        effective_m = new_m
        if bias_correction > 0.0:
            effective_m = new_m / bias_correction

        delta = lr * effective_m
        new_val = round(current_val + delta, 4)

        if bounds is not None:
            min_v, max_v = bounds
            new_val = max(min_v, min(max_v, new_val))

        meta["momentum"] = round(new_m, 6)
        meta["step"] = new_step
        meta["last_gradient"] = round(gradient, 6)
        meta["last_delta"] = round(delta, 6)

        self.set(name, new_val, reason=reason, metadata=meta)
        return new_val

    def apply_ema(
        self,
        name: str,
        candidate_value: float,
        *,
        alpha: float | None = None,
        bounds: tuple[float, float] | None = None,
        reason: str = "ema_smoothing",
    ) -> float:
        """Update parameter via Exponential Moving Average (EMA)."""
        current_val = self.get(name)
        a = self.get("parameter_ema_alpha", 0.80)
        if alpha is not None:
            a = float(alpha)
        smoothed = round(a * current_val + (1.0 - a) * candidate_value, 4)
        if bounds is not None:
            min_v, max_v = bounds
            smoothed = max(min_v, min(max_v, smoothed))
        all_params = self.list_all()
        meta = dict(all_params.get(name, {}).get("metadata", {}))
        meta["ema_source_candidate"] = candidate_value
        meta["ema_alpha"] = a
        self.set(name, smoothed, reason=reason, metadata=meta)
        return smoothed

    def list_all(self) -> dict[str, dict[str, Any]]:
        self._sync()
        entries = self.provider.list_parameters()
        return {
            name: {
                "name": name,
                "value": val,
                "updated_at": updated_at,
                "metadata": meta,
            }
            for name, (val, updated_at, meta) in entries.items()
        }
