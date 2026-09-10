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

    def to_dict(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "baseline_value": self.baseline_value,
            "candidate_value": self.candidate_value,
            "hypothesis": self.hypothesis,
            "eval_case_ids": list(self.eval_case_ids),
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
    "confidence_reduction_delta": (0.02, 0.30, 0.02),
}


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
        for attr in attributions:
            name = attr.target_id
            if attr.node_type == "dynamic_parameter" and name in _PARAMETER_EXPLORATION_BOUNDS:
                param_attribution_counts[name] = param_attribution_counts.get(name, 0) + 1

        ordered_targets: list[str] = sorted(
            _PARAMETER_EXPLORATION_BOUNDS.keys(),
            key=lambda k: param_attribution_counts.get(k, 0),
            reverse=True,
        )

        specs: list[ShadowTrialSpec] = []
        for target_id in ordered_targets:
            if len(specs) >= limit:
                break
            min_val, max_val, step = _PARAMETER_EXPLORATION_BOUNDS[target_id]
            current_val = self.param_registry.get(target_id)
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

    def execute_shadow_trial(
        self,
        spec: ShadowTrialSpec,
        *,
        auto_promote: bool = True,
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
            # Positive score delta indicating passed verification and simulated utility
            score_delta = 0.10

        promoted = passed and auto_promote
        if promoted:
            self.param_registry.set(
                spec.target_id,
                spec.candidate_value,
                reason=f"self_play_promoted:{spec.trial_id}",
            )

        evidence = {
            "checks": all_checks,
            "hypothesis": spec.hypothesis,
            "target_type": spec.target_type,
            "target_id": spec.target_id,
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
    ) -> list[ShadowTrialResult]:
        """Execute a full autonomous exploration and verification cycle."""
        specs = self.generate_parameter_perturbations(limit=max_trials)
        results: list[ShadowTrialResult] = []
        for spec in specs:
            res = self.execute_shadow_trial(spec, auto_promote=auto_promote)
            results.append(res)
        return results
