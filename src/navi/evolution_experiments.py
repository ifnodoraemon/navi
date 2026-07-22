from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Any

from .db import connect
from .evolution import EvolutionLedger
from .evolution_targets import EvolutionTargetAdapterRegistry
from .paths import db_paths


@dataclass(frozen=True, slots=True)
class EvolutionExperiment:
    id: str
    proposal_id: str
    status: str
    baseline_fingerprint: str
    candidate_fingerprint: str
    eval_cases_json: str
    checks_json: str
    created_at: float
    completed_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EvolutionActivation:
    id: str
    proposal_id: str
    event_id: str
    status: str
    success_count: int
    error_count: int
    observation_count: int
    min_observations: int
    max_error_rate: float
    latest_evidence_json: str
    rollback_event_id: str
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EvolutionExperimentStore:
    def __init__(self, home: Path):
        self.home = home
        self.db_path = db_paths(home).evolution
        self.targets = EvolutionTargetAdapterRegistry(home)
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evolution_experiments (
                    id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    baseline_fingerprint TEXT NOT NULL,
                    candidate_fingerprint TEXT NOT NULL,
                    eval_cases_json TEXT NOT NULL,
                    checks_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    completed_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_evolution_experiments_proposal "
                "ON evolution_experiments(proposal_id, created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evolution_activations (
                    id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    success_count INTEGER NOT NULL,
                    error_count INTEGER NOT NULL,
                    observation_count INTEGER NOT NULL,
                    min_observations INTEGER NOT NULL,
                    max_error_rate REAL NOT NULL,
                    latest_evidence_json TEXT NOT NULL,
                    rollback_event_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_evolution_activations_status "
                "ON evolution_activations(status, updated_at)"
            )

    def run(self, proposal_id: str) -> EvolutionExperiment:
        proposal = EvolutionLedger(self.home).get_proposal(proposal_id)
        if proposal is None:
            raise KeyError("evolution proposal not found")
        if proposal.status != "proposed":
            raise ValueError(f"cannot experiment on proposal in status: {proposal.status}")
        adapter = self.targets.get(proposal.target_type)
        current = adapter.read(proposal.target_id)
        checks: list[dict[str, Any]] = []
        checks.append(
            {
                "check": "baseline_matches",
                "passed": current == proposal.before,
            }
        )
        try:
            validation = adapter.validate(proposal.target_id, proposal.after)
            checks.append(
                {"check": "adapter_validation", "passed": True, "facts": validation}
            )
        except Exception as exc:
            checks.append(
                {
                    "check": "adapter_validation",
                    "passed": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
        eval_cases = _json_list(proposal.eval_cases)
        if not eval_cases:
            checks.append({"check": "eval_cases_declared", "passed": False})
        for case_id in eval_cases:
            checks.extend(self._evaluate_case(case_id, proposal.target_type, proposal.after))
        status = "passed" if checks and all(bool(item.get("passed")) for item in checks) else "failed"
        experiment = EvolutionExperiment(
            id=uuid.uuid4().hex,
            proposal_id=proposal.id,
            status=status,
            baseline_fingerprint=_fingerprint(current),
            candidate_fingerprint=_fingerprint(proposal.after),
            eval_cases_json=json.dumps(eval_cases, sort_keys=True),
            checks_json=json.dumps(checks, ensure_ascii=False, sort_keys=True),
            created_at=time.time(),
            completed_at=time.time(),
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO evolution_experiments(
                    id, proposal_id, status, baseline_fingerprint,
                    candidate_fingerprint, eval_cases_json, checks_json,
                    created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(asdict(experiment).values()),
            )
        return experiment

    def _evaluate_case(
        self,
        case_id: str,
        target_type: str,
        candidate: str,
    ) -> list[dict[str, Any]]:
        try:
            raw = self.targets.get("eval_case").read(case_id)
            if not raw:
                raise ValueError("eval case not found")
            case = json.loads(raw)
            if not isinstance(case, dict):
                raise ValueError("eval case must be an object")
            allowed = case.get("target_types")
            if isinstance(allowed, list) and target_type not in allowed:
                raise ValueError("eval case does not apply to proposal target_type")
            assertions = case.get("assertions")
            if not isinstance(assertions, list) or not assertions:
                raise ValueError("eval case assertions are required")
        except Exception as exc:
            return [
                {
                    "check": "eval_case_load",
                    "case_id": case_id,
                    "passed": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            ]
        return [
            {
                "check": "eval_case_fingerprint",
                "case_id": case_id,
                "fingerprint": _fingerprint(raw),
                "passed": True,
            },
            *[
                _evaluate_assertion(case_id, assertion, candidate)
                for assertion in assertions
            ],
        ]

    def latest_experiment(self, proposal_id: str) -> EvolutionExperiment | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, proposal_id, status, baseline_fingerprint,
                       candidate_fingerprint, eval_cases_json, checks_json,
                       created_at, completed_at
                FROM evolution_experiments WHERE proposal_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (proposal_id,),
            ).fetchone()
        return EvolutionExperiment(*row) if row else None

    def assert_passed(self, proposal_id: str, *, candidate: str) -> None:
        experiment = self.latest_experiment(proposal_id)
        if experiment is None or experiment.status != "passed":
            raise ValueError("proposal requires a passed persisted experiment before apply")
        if experiment.candidate_fingerprint != _fingerprint(candidate):
            raise ValueError("proposal candidate changed after its experiment")
        eval_cases = _json_list(experiment.eval_cases_json)
        checks = _json_objects(experiment.checks_json)
        fingerprints = {
            str(check.get("case_id") or ""): str(check.get("fingerprint") or "")
            for check in checks
            if check.get("check") == "eval_case_fingerprint"
        }
        for case_id in eval_cases:
            raw = self.targets.get("eval_case").read(case_id)
            if not raw or fingerprints.get(case_id) != _fingerprint(raw):
                raise ValueError(
                    f"evaluation case changed after experiment: {case_id}"
                )

    def start_activation(
        self,
        *,
        proposal_id: str,
        event_id: str,
        min_observations: int = 3,
        max_error_rate: float = 0.2,
    ) -> EvolutionActivation:
        now = time.time()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO evolution_activations(
                    id, proposal_id, event_id, status, success_count, error_count,
                    observation_count, min_observations, max_error_rate,
                    latest_evidence_json, rollback_event_id, created_at, updated_at
                ) VALUES (?, ?, ?, 'observing', 0, 0, 0, ?, ?, '{}', '', ?, ?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (
                    uuid.uuid4().hex,
                    proposal_id,
                    event_id,
                    max(1, min_observations),
                    max(0.0, min(1.0, max_error_rate)),
                    now,
                    now,
                ),
            )
        activation = self.activation_for_event(event_id)
        if activation is None:
            raise RuntimeError("evolution activation was not persisted")
        return activation

    def observe(
        self,
        event_id: str,
        *,
        successes: int,
        errors: int,
        evidence: dict[str, Any] | None = None,
        rollback: Callable[[str], Any] | None = None,
    ) -> EvolutionActivation:
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id, proposal_id, event_id, status, success_count, error_count,
                       observation_count, min_observations, max_error_rate,
                       latest_evidence_json, rollback_event_id, created_at, updated_at
                FROM evolution_activations WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
            if row is None:
                raise KeyError("evolution activation not found")
            activation = EvolutionActivation(*row)
            if activation.status != "observing":
                needs_rollback = activation.status == "regressed"
            else:
                success_count = activation.success_count + max(0, int(successes))
                error_count = activation.error_count + max(0, int(errors))
                observations = activation.observation_count + 1
                total = success_count + error_count
                error_rate = error_count / total if total else 0.0
                status = "observing"
                if observations >= activation.min_observations:
                    status = (
                        "regressed"
                        if error_rate > activation.max_error_rate
                        else "healthy"
                    )
                now = time.time()
                cursor = conn.execute(
                """
                UPDATE evolution_activations
                SET status = ?, success_count = ?, error_count = ?, observation_count = ?,
                    latest_evidence_json = ?, updated_at = ?
                WHERE id = ? AND status = 'observing'
                """,
                (
                    status,
                    success_count,
                    error_count,
                    observations,
                    json.dumps(
                        {**(evidence or {}), "error_rate": error_rate},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                    activation.id,
                ),
            )
                if cursor.rowcount != 1:
                    raise RuntimeError("evolution activation changed concurrently")
                needs_rollback = status == "regressed"
        if needs_rollback:
            if rollback is None:
                raise RuntimeError("regressed evolution activation requires a rollback port")
            rolled_back = rollback(event_id)
            rollback_event_id = rolled_back.id if rolled_back else ""
            with connect(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE evolution_activations
                    SET status = 'rolled_back', rollback_event_id = ?, updated_at = ?
                    WHERE id = ? AND status = 'regressed'
                    """,
                    (rollback_event_id, time.time(), activation.id),
                )
        refreshed = self.activation_for_event(event_id)
        if refreshed is None:
            raise RuntimeError("evolution activation disappeared after observation")
        return refreshed

    def activation_for_event(self, event_id: str) -> EvolutionActivation | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, proposal_id, event_id, status, success_count, error_count,
                       observation_count, min_observations, max_error_rate,
                       latest_evidence_json, rollback_event_id, created_at, updated_at
                FROM evolution_activations WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        return EvolutionActivation(*row) if row else None

    def mark_rolled_back(
        self,
        event_id: str,
        *,
        rollback_event_id: str = "",
    ) -> EvolutionActivation | None:
        now = time.time()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE evolution_activations
                SET status = 'rolled_back', rollback_event_id = ?, updated_at = ?
                WHERE event_id = ?
                """,
                (rollback_event_id, now, event_id),
            )
        return self.activation_for_event(event_id)

    def list_activations(self, *, status: str = "", limit: int = 100) -> list[EvolutionActivation]:
        with connect(self.db_path) as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT id, proposal_id, event_id, status, success_count, error_count,
                           observation_count, min_observations, max_error_rate,
                           latest_evidence_json, rollback_event_id, created_at, updated_at
                    FROM evolution_activations WHERE status = ?
                    ORDER BY updated_at ASC LIMIT ?
                    """,
                    (status, max(1, limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, proposal_id, event_id, status, success_count, error_count,
                           observation_count, min_observations, max_error_rate,
                           latest_evidence_json, rollback_event_id, created_at, updated_at
                    FROM evolution_activations ORDER BY updated_at DESC LIMIT ?
                    """,
                    (max(1, limit),),
                ).fetchall()
        return [EvolutionActivation(*row) for row in rows]


def _evaluate_assertion(case_id: str, raw: Any, candidate: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"check": "assertion", "case_id": case_id, "passed": False, "error": "invalid"}
    kind = str(raw.get("type") or "")
    value = str(raw.get("value") or "")
    passed = False
    if kind == "contains":
        passed = bool(value) and value in candidate
    elif kind == "not_contains":
        passed = bool(value) and value not in candidate
    elif kind == "json_valid":
        try:
            json.loads(candidate)
            passed = True
        except json.JSONDecodeError:
            passed = False
    else:
        return {
            "check": "assertion",
            "case_id": case_id,
            "assertion_type": kind,
            "passed": False,
            "error": "unsupported assertion type",
        }
    return {
        "check": "assertion",
        "case_id": case_id,
        "assertion_type": kind,
        "passed": passed,
    }


def _json_list(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _json_objects(value: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
