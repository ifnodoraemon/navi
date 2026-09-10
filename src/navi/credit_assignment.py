"""Causal credit assignment engine: backpropagates rewards and penalties along causal trace DAG.

Models agent execution as an Agentic Neural Network:
Forward pass: Context (Memories + Prompt Blocks + Parameters) -> Action -> Outcome.
Backward pass: Loss / Terminal Reward -> Causal Credit Attribution -> Synaptic Plasticity.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any
import uuid

from .db import connect
from .dynamic_parameters import DynamicParameterRegistry
from .loop import TraceFailureDomain, TraceOutcome, TracePhase
from .memory.store import MemoryStore
from .paths import db_paths
from .schema import Column, Table, assert_schema_exact


CAUSAL_CREDIT_ATTRIBUTIONS_TABLE = Table(
    "causal_credit_attributions",
    [
        Column("id", "TEXT", primary_key=True),
        Column("trace_id", "TEXT", nullable=False),
        Column("node_type", "TEXT", nullable=False),
        Column("target_id", "TEXT", nullable=False),
        Column("outcome", "TEXT", nullable=False),
        Column("failure_domain", "TEXT", nullable=False),
        Column("reward", "REAL", nullable=False),
        Column("delta_applied", "REAL", nullable=False),
        Column("reason", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
    ],
)


@dataclass(frozen=True)
class CausalCreditAttribution:
    id: str
    trace_id: str
    node_type: str
    target_id: str
    outcome: str
    failure_domain: str
    reward: float
    delta_applied: float
    reason: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "trace_id": self.trace_id,
            "node_type": self.node_type,
            "target_id": self.target_id,
            "outcome": self.outcome,
            "failure_domain": self.failure_domain,
            "reward": self.reward,
            "delta_applied": self.delta_applied,
            "reason": self.reason,
            "created_at": self.created_at,
        }


_FAILURE_DOMAIN_SEVERITY: dict[str, float] = {
    str(TraceFailureDomain.SAFEGUARD_POLICY): 1.0,
    str(TraceFailureDomain.LOOP_NO_PROGRESS): 0.8,
    str(TraceFailureDomain.PLANNER_OR_PARSER): 0.7,
    str(TraceFailureDomain.CHECKER_BLOCKED): 0.6,
    str(TraceFailureDomain.MISUNDERSTANDING): 0.6,
    str(TraceFailureDomain.CAPABILITY_FAILURE): 0.5,
    str(TraceFailureDomain.RUNTIME): 0.4,
    str(TraceFailureDomain.PROVIDER_NO_RESPONSE): 0.3,
    str(TraceFailureDomain.NONE): 0.0,
}


def compute_terminal_reward(
    outcome: str,
    failure_domain: str,
    param_registry: DynamicParameterRegistry | None = None,
) -> float:
    """Compute normalized scalar reward R in [-1.0, 1.0]."""
    success_reward = 1.0
    degraded_reward = 0.2
    default_severity = _FAILURE_DOMAIN_SEVERITY.get(failure_domain, 0.5)
    if param_registry is not None:
        success_reward = param_registry.get("reward_success", 1.0)
        degraded_reward = param_registry.get("reward_degraded", 0.2)
        severity_key = f"severity_{failure_domain}"
        default_severity = param_registry.get(severity_key, default_severity)
    if outcome == str(TraceOutcome.SUCCESS):
        return success_reward
    if outcome == str(TraceOutcome.DEGRADED):
        return degraded_reward
    return -1.0 * default_severity


class CreditAssignmentEngine:
    """Backpropagates trace evaluations along the causal execution graph."""

    def __init__(self, home: Path):
        self.home = home
        self.db_path = db_paths(home).traces
        self.memory_store = MemoryStore(home)
        self.param_registry = DynamicParameterRegistry(home)
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.execute(CAUSAL_CREDIT_ATTRIBUTIONS_TABLE.ddl)
            assert_schema_exact(conn, CAUSAL_CREDIT_ATTRIBUTIONS_TABLE)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_causal_credit_trace "
                "ON causal_credit_attributions(trace_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_causal_credit_target "
                "ON causal_credit_attributions(node_type, target_id)"
            )

    def record_attribution(
        self,
        *,
        trace_id: str,
        node_type: str,
        target_id: str,
        outcome: str,
        failure_domain: str,
        reward: float,
        delta_applied: float,
        reason: str,
        now: float | None = None,
    ) -> CausalCreditAttribution:
        current_time = time.time()
        if now is not None:
            current_time = float(now)
        attr = CausalCreditAttribution(
            id=uuid.uuid4().hex,
            trace_id=trace_id,
            node_type=node_type,
            target_id=target_id,
            outcome=outcome,
            failure_domain=failure_domain,
            reward=float(reward),
            delta_applied=float(delta_applied),
            reason=reason,
            created_at=current_time,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO causal_credit_attributions (
                    id, trace_id, node_type, target_id, outcome,
                    failure_domain, reward, delta_applied, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attr.id,
                    attr.trace_id,
                    attr.node_type,
                    attr.target_id,
                    attr.outcome,
                    attr.failure_domain,
                    attr.reward,
                    attr.delta_applied,
                    attr.reason,
                    attr.created_at,
                ),
            )
        return attr

    def list_attributions(
        self,
        *,
        trace_id: str | None = None,
        node_type: str | None = None,
        target_id: str | None = None,
        limit: int = 100,
    ) -> list[CausalCreditAttribution]:
        predicates: list[str] = []
        params: list[Any] = []
        if trace_id is not None:
            predicates.append("trace_id = ?")
            params.append(trace_id)
        if node_type is not None:
            predicates.append("node_type = ?")
            params.append(node_type)
        if target_id is not None:
            predicates.append("target_id = ?")
            params.append(target_id)
        where_clause = ""
        if predicates:
            where_clause = f"WHERE {' AND '.join(predicates)}"
        sql = f"""
            SELECT id, trace_id, node_type, target_id, outcome,
                   failure_domain, reward, delta_applied, reason, created_at
            FROM causal_credit_attributions
            {where_clause}
            ORDER BY created_at DESC
            LIMIT ?
        """
        params.append(limit)
        with connect(self.db_path) as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            CausalCreditAttribution(
                id=row[0],
                trace_id=row[1],
                node_type=row[2],
                target_id=row[3],
                outcome=row[4],
                failure_domain=row[5],
                reward=float(row[6]),
                delta_applied=float(row[7]),
                reason=row[8],
                created_at=float(row[9]),
            )
            for row in rows
        ]

    def backprop_trace(
        self,
        *,
        trace_id: str,
        outcome: str,
        failure_domain: str,
        events: list[Any],
        evidence: dict[str, Any] | None = None,
        now: float | None = None,
        custom_reward: float | None = None,
    ) -> list[CausalCreditAttribution]:
        """Execute backward attribution pass on the trace execution graph."""
        if not events:
            return []
        reward = compute_terminal_reward(outcome, failure_domain, param_registry=self.param_registry)
        if custom_reward is not None:
            reward = float(custom_reward)
        if evidence is not None and custom_reward is None:
            cr = evidence.get("custom_reward")
            if cr is not None:
                reward = float(cr)
            rw = evidence.get("reward")
            if rw is not None and cr is None:
                reward = float(rw)
        attributions: list[CausalCreditAttribution] = []

        # 1. Extract used memory items across planner syscall events with temporal discounting
        syscall_events: list[Any] = [
            e for e in events
            if getattr(e, "phase", "") in {str(TracePhase.PLANNER_SYSCALL), "planner.syscall"}
        ]
        if not syscall_events:
            syscall_events = [
                e for e in events
                if "used_memory_ids" in (getattr(e, "output_json", "") or "")
            ]

        total_steps = len(syscall_events)
        gamma = self.param_registry.get("temporal_discount_factor", 0.85)

        # Map each memory_id to its highest effective temporal discount factor
        memory_discounts: dict[str, float] = {}
        for step_idx, event in enumerate(syscall_events):
            output_json = getattr(event, "output_json", "") or ""
            if not output_json:
                continue
            try:
                data = json.loads(output_json)
                if isinstance(data, dict):
                    mems = data.get("used_memory_ids", [])
                    if isinstance(mems, list):
                        steps_from_end = max(0, total_steps - 1 - step_idx)
                        step_discount = round(gamma ** steps_from_end, 4)
                        for m in mems:
                            clean_m = str(m).strip()
                            if clean_m:
                                current_best = memory_discounts.get(clean_m, 0.0)
                                memory_discounts[clean_m] = max(current_best, step_discount)
            except Exception:
                continue

        # 2. Backprop to Memory Items with TD discount
        ltp_boost = self.param_registry.get("ltp_boost_delta", 0.05)
        conf_drop = self.param_registry.get("confidence_reduction_delta", 0.10)
        for mem_id, discount in sorted(memory_discounts.items()):
            effective_reward = reward * discount
            delta = round(ltp_boost * effective_reward, 4)
            if reward < 0:
                delta = round(-1.0 * conf_drop * abs(reward) * discount, 4)
            applied = self.memory_store.apply_credit_delta(
                mem_id,
                delta,
                reason=f"credit_attribution:{outcome}:{failure_domain}:gamma_{discount:.2f}",
                now=now,
            )
            if applied is not None:
                attributions.append(
                    self.record_attribution(
                        trace_id=trace_id,
                        node_type="memory",
                        target_id=mem_id,
                        outcome=outcome,
                        failure_domain=failure_domain,
                        reward=effective_reward,
                        delta_applied=delta,
                        reason=f"td_credit_attribution(gamma={discount:.2f},confidence={applied.confidence:.2f})",
                        now=now,
                    )
                )

        # 3. Backprop to Dynamic Parameters via Adam/momentum gradient
        if failure_domain == str(TraceFailureDomain.PROVIDER_NO_RESPONSE):
            current_retry = self.param_registry.get("provider_retry_after_seconds", 15.0)
            new_retry = self.param_registry.apply_gradient(
                "provider_retry_after_seconds",
                gradient=2.0,
                learning_rate=1.0,
                bounds=(5.0, 60.0),
                reason=f"backprop:provider_congestion from trace {trace_id}",
            )
            attributions.append(
                self.record_attribution(
                    trace_id=trace_id,
                    node_type="dynamic_parameter",
                    target_id="provider_retry_after_seconds",
                    outcome=outcome,
                    failure_domain=failure_domain,
                    reward=reward,
                    delta_applied=round(new_retry - current_retry, 4),
                    reason=f"backoff_adjusted_to_{new_retry:.1f}s",
                    now=now,
                )
            )

        # 4. Backprop to Prompt Layers on planner/parser/misunderstanding failures
        if failure_domain in {
            str(TraceFailureDomain.PLANNER_OR_PARSER),
            str(TraceFailureDomain.CHECKER_BLOCKED),
            str(TraceFailureDomain.MISUNDERSTANDING),
            str(TraceFailureDomain.LOOP_NO_PROGRESS),
        }:
            target_layer = "instructions"
            attributions.append(
                self.record_attribution(
                    trace_id=trace_id,
                    node_type="prompt_layer",
                    target_id=target_layer,
                    outcome=outcome,
                    failure_domain=failure_domain,
                    reward=reward,
                    delta_applied=reward,
                    reason=f"blame_registered_for_{failure_domain}",
                    now=now,
                )
            )

        # 5. Backprop to Tool nodes on capability failures
        if failure_domain == str(TraceFailureDomain.CAPABILITY_FAILURE):
            failed_tools = {
                str(getattr(e, "tool", "") or "").strip()
                for e in events
                if not bool(getattr(e, "ok", True)) and str(getattr(e, "tool", "") or "").strip()
            }
            for tool_name in sorted(failed_tools):
                attributions.append(
                    self.record_attribution(
                        trace_id=trace_id,
                        node_type="tool",
                        target_id=tool_name,
                        outcome=outcome,
                        failure_domain=failure_domain,
                        reward=reward,
                        delta_applied=reward,
                        reason=f"blame_registered_for_capability_failure:{tool_name}",
                        now=now,
                    )
                )

        # 6. Backprop on Loop No Progress failures
        if failure_domain == str(TraceFailureDomain.LOOP_NO_PROGRESS):
            attributions.append(
                self.record_attribution(
                    trace_id=trace_id,
                    node_type="prompt_layer",
                    target_id="instructions",
                    outcome=outcome,
                    failure_domain=failure_domain,
                    reward=reward,
                    delta_applied=reward,
                    reason="blame_registered_for_loop_no_progress",
                    now=now,
                )
            )
            attributions.append(
                self.record_attribution(
                    trace_id=trace_id,
                    node_type="dynamic_parameter",
                    target_id="loop_max_attempts_turn",
                    outcome=outcome,
                    failure_domain=failure_domain,
                    reward=reward,
                    delta_applied=reward,
                    reason="budget_strain_for_loop_no_progress",
                    now=now,
                )
            )

        # 7. Backprop on Safeguard Policy violations
        if failure_domain == str(TraceFailureDomain.SAFEGUARD_POLICY):
            attributions.append(
                self.record_attribution(
                    trace_id=trace_id,
                    node_type="prompt_layer",
                    target_id="instructions",
                    outcome=outcome,
                    failure_domain=failure_domain,
                    reward=reward,
                    delta_applied=reward,
                    reason="blame_registered_for_safeguard_violation",
                    now=now,
                )
            )
            attributions.append(
                self.record_attribution(
                    trace_id=trace_id,
                    node_type="dynamic_parameter",
                    target_id="safeguards_entropy_threshold",
                    outcome=outcome,
                    failure_domain=failure_domain,
                    reward=reward,
                    delta_applied=reward,
                    reason="safeguards_entropy_threshold_scrutiny",
                    now=now,
                )
            )

        # 8. Forward credit reinforcement on successful traces
        if outcome == str(TraceOutcome.SUCCESS):
            successful_tools = {
                str(getattr(e, "tool", "") or "").strip()
                for e in events
                if bool(getattr(e, "ok", True))
                and getattr(e, "phase", "") in {str(TracePhase.CAPABILITY_RESULT), "capability.result"}
                and str(getattr(e, "tool", "") or "").strip()
            }
            for tool_name in sorted(successful_tools):
                attributions.append(
                    self.record_attribution(
                        trace_id=trace_id,
                        node_type="tool",
                        target_id=tool_name,
                        outcome=outcome,
                        failure_domain=failure_domain,
                        reward=reward,
                        delta_applied=reward,
                        reason=f"positive_credit_attributed_for_tool:{tool_name}",
                        now=now,
                    )
                )

        # 9. Ingest evaluated trace into Multi-Channel Experience Replay Buffer
        try:
            from .replay_buffer import ExperienceReplayBuffer

            replay_buffer = ExperienceReplayBuffer(self.home)
            replay_buffer.ingest_from_trace(
                trace_id=trace_id,
                outcome=outcome,
                failure_domain=failure_domain,
                events=events,
                evidence=evidence,
                now=now,
                custom_reward=reward,
            )
        except Exception:
            pass

        return attributions
