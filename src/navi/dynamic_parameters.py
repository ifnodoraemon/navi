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

    # Self-play and prompt evolution parameters
    "prompt_mutation_temperature": 0.70,
    "prompt_evaluation_threshold": 0.80,
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
