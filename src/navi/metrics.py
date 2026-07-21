from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .db import connect
from .effect_journal import EffectJournal
from .evolution import EvolutionLedger
from .evolution_experiments import EvolutionExperimentStore
from .goals import GoalStore
from .identity import IdentityStore
from .lifecycle_saga import LifecycleSagaStore
from .loop_runs import LoopRunStore
from .memory import MemoryStore
from .paths import db_paths
from .personal_resources import PersonalResourceStore
from .resource_gateway import SQLiteResourceLedger
from .runs import RunStore
from .trace import TraceStore


@dataclass(frozen=True, slots=True)
class MetricFact:
    name: str
    value: float
    unit: str
    samples: int
    window_seconds: float
    source: str


@dataclass(frozen=True, slots=True)
class SLOFact:
    name: str
    status: str
    target: str
    actual: float
    samples: int
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SystemMetricsSnapshot:
    generated_at: float
    overall_status: str
    metrics: tuple[MetricFact, ...]
    slos: tuple[SLOFact, ...]
    diagnostics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "overall_status": self.overall_status,
            "metrics": [asdict(item) for item in self.metrics],
            "slos": [asdict(item) for item in self.slos],
            "diagnostics": self.diagnostics,
        }


class MetricsProjector:
    """Read-only projection of durable runtime facts into metrics and SLOs."""

    def __init__(self, home: Path):
        self.home = home
        self.paths = db_paths(home)
        RunStore(home)
        GoalStore(home)
        LoopRunStore(home)
        EffectJournal(home)
        LifecycleSagaStore(home)
        MemoryStore(home)
        IdentityStore(home)
        TraceStore(home)
        EvolutionLedger(home)
        EvolutionExperimentStore(home)
        SQLiteResourceLedger(home)
        PersonalResourceStore(home)

    def snapshot(self, *, now: float | None = None, window_seconds: float = 604800) -> SystemMetricsSnapshot:
        current_time = time.time() if now is None else now
        cutoff = current_time - max(60.0, window_seconds)
        runs = self._run_metrics(cutoff)
        traces = self._trace_metrics(cutoff)
        integrity = self._integrity_metrics(current_time)
        pipeline = self._pipeline_metrics(current_time)
        metrics = (
            MetricFact(
                "task_success_rate",
                runs["success_rate"],
                "ratio",
                runs["terminal"],
                window_seconds,
                "runs.db",
            ),
            MetricFact(
                "trace_failure_rate",
                traces["failure_rate"],
                "ratio",
                traces["evaluated"],
                window_seconds,
                "traces.db",
            ),
            MetricFact(
                "lifecycle_orphan_count",
                float(integrity["orphan_count"]),
                "count",
                integrity["records"],
                0,
                "runs.db+goals.db+loop_runs.db",
            ),
            MetricFact(
                "uncertain_effect_count",
                float(integrity["uncertain_effects"]),
                "count",
                integrity["effects"],
                0,
                "loop_runs.db",
            ),
            MetricFact(
                "expired_execution_lease_count",
                float(integrity["expired_leases"]),
                "count",
                integrity["active_loops"],
                0,
                "loop_runs.db",
            ),
            MetricFact(
                "stale_resource_grant_count",
                float(integrity["stale_resource_grants"]),
                "count",
                integrity["resource_grants"],
                0,
                "resource_ledger.db",
            ),
            MetricFact(
                "memory_pipeline_failure_count",
                float(pipeline["memory_failures"]),
                "count",
                pipeline["memory_jobs"],
                0,
                "memory.db",
            ),
        )
        slos = (
            _zero_slo(
                "lifecycle_projection_integrity",
                integrity["orphan_count"] + integrity["pending_sagas"],
                evidence={
                    "orphan_count": integrity["orphan_count"],
                    "pending_sagas": integrity["pending_sagas"],
                },
            ),
            _zero_slo(
                "mutating_effect_certainty",
                integrity["uncertain_effects"],
                evidence={"uncertain_effects": integrity["uncertain_effects"]},
            ),
            _zero_slo(
                "execution_lease_health",
                integrity["expired_leases"],
                evidence={"expired_leases": integrity["expired_leases"]},
            ),
            _zero_slo(
                "resource_release_integrity",
                integrity["stale_resource_grants"],
                evidence={"stale_resource_grants": integrity["stale_resource_grants"]},
            ),
            _zero_slo(
                "memory_pipeline_health",
                pipeline["memory_failures"],
                evidence={
                    "failed_or_stale_jobs": pipeline["memory_failures"],
                    "pending_jobs": pipeline["memory_pending"],
                },
            ),
            _zero_slo(
                "evolution_activation_safety",
                pipeline["regressed_activations"],
                evidence={
                    "regressed": pipeline["regressed_activations"],
                    "observing": pipeline["observing_activations"],
                },
            ),
            _ratio_slo(
                "task_success_rate",
                runs["success_rate"],
                samples=runs["terminal"],
                minimum_samples=5,
                target=0.8,
                higher_is_better=True,
            ),
            _ratio_slo(
                "trace_failure_rate",
                traces["failure_rate"],
                samples=traces["evaluated"],
                minimum_samples=5,
                target=0.1,
                higher_is_better=False,
            ),
        )
        overall = "breached" if any(item.status == "breached" for item in slos) else "met"
        if overall == "met" and any(item.status == "insufficient_data" for item in slos):
            overall = "insufficient_data"
        diagnostics = {
            **integrity,
            **pipeline,
            "terminal_runs": runs["terminal"],
            "successful_runs": runs["successful"],
            "evaluated_traces": traces["evaluated"],
            "failed_traces": traces["failed"],
        }
        return SystemMetricsSnapshot(
            generated_at=current_time,
            overall_status=overall,
            metrics=metrics,
            slos=slos,
            diagnostics=diagnostics,
        )

    def observe_evolution_activations(self, *, now: float | None = None) -> list[dict[str, Any]]:
        current_time = time.time() if now is None else now
        store = EvolutionExperimentStore(self.home)
        observations: list[dict[str, Any]] = []
        for activation in store.list_activations(status="observing", limit=100):
            with connect(self.paths.runs) as conn:
                row = conn.execute(
                    """
                    SELECT
                        SUM(CASE WHEN resolution = 'success' THEN 1 ELSE 0 END),
                        SUM(CASE WHEN resolution IN ('failed', 'blocked') THEN 1 ELSE 0 END)
                    FROM runs
                    WHERE phase = 'ended' AND updated_at > ? AND updated_at <= ?
                    """,
                    (activation.updated_at, current_time),
                ).fetchone()
            successes = int(row[0] or 0) if row else 0
            errors = int(row[1] or 0) if row else 0
            if successes + errors == 0:
                continue
            observed = store.observe(
                activation.event_id,
                successes=successes,
                errors=errors,
                evidence={
                    "source": "system_canary_window",
                    "window_start": activation.updated_at,
                    "window_end": current_time,
                },
            )
            observations.append(observed.to_dict())
        return observations

    def _run_metrics(self, cutoff: float) -> dict[str, Any]:
        with connect(self.paths.runs) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*), SUM(CASE WHEN resolution = 'success' THEN 1 ELSE 0 END)
                FROM runs WHERE phase = 'ended' AND updated_at >= ?
                """,
                (cutoff,),
            ).fetchone()
        terminal = int(row[0] or 0) if row else 0
        successful = int(row[1] or 0) if row else 0
        return {
            "terminal": terminal,
            "successful": successful,
            "success_rate": successful / terminal if terminal else 0.0,
        }

    def _trace_metrics(self, cutoff: float) -> dict[str, Any]:
        with connect(self.paths.traces) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*), SUM(CASE WHEN outcome = 'failure' THEN 1 ELSE 0 END)
                FROM trace_evaluations WHERE created_at >= ?
                """,
                (cutoff,),
            ).fetchone()
        evaluated = int(row[0] or 0) if row else 0
        failed = int(row[1] or 0) if row else 0
        return {
            "evaluated": evaluated,
            "failed": failed,
            "failure_rate": failed / evaluated if evaluated else 0.0,
        }

    def _integrity_metrics(self, now: float) -> dict[str, Any]:
        with connect(self.paths.runs) as conn:
            run_ids = {str(row[0]) for row in conn.execute("SELECT id FROM runs")}
        with connect(self.paths.goals) as conn:
            goal_rows = conn.execute("SELECT id, run_id FROM goals").fetchall()
        goal_ids = {str(row[0]) for row in goal_rows}
        missing_runs = [str(row[0]) for row in goal_rows if str(row[1]) not in run_ids]
        with connect(self.paths.loop_runs) as conn:
            loop_rows = conn.execute("SELECT id, goal_id FROM loop_runs").fetchall()
            expired_leases = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM loop_runs
                    WHERE terminal_state = '' AND lease_owner != '' AND lease_expires_at <= ?
                    """,
                    (now,),
                ).fetchone()[0]
            )
            active_loops = int(
                conn.execute(
                    "SELECT COUNT(*) FROM loop_runs WHERE terminal_state = ''"
                ).fetchone()[0]
            )
            uncertain_effects = int(
                conn.execute("SELECT COUNT(*) FROM loop_effects WHERE status = 'uncertain'").fetchone()[0]
            )
            effects = int(conn.execute("SELECT COUNT(*) FROM loop_effects").fetchone()[0])
            pending_sagas = int(
                conn.execute(
                    "SELECT COUNT(*) FROM lifecycle_sagas WHERE status IN ('pending', 'failed')"
                ).fetchone()[0]
            )
        missing_goals = [str(row[0]) for row in loop_rows if str(row[1]) not in goal_ids]
        stale_before = now - 900
        with connect(self.paths.resource_ledger) as conn:
            stale_resource_grants = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM resource_grants
                    WHERE status = 'active' AND updated_at <= ?
                    """,
                    (stale_before,),
                ).fetchone()[0]
            )
            resource_grants = int(conn.execute("SELECT COUNT(*) FROM resource_grants").fetchone()[0])
        return {
            "records": len(run_ids) + len(goal_rows) + len(loop_rows),
            "orphan_count": len(missing_runs) + len(missing_goals),
            "orphan_goal_ids": missing_runs[:20],
            "orphan_loop_run_ids": missing_goals[:20],
            "active_loops": active_loops,
            "expired_leases": expired_leases,
            "uncertain_effects": uncertain_effects,
            "effects": effects,
            "pending_sagas": pending_sagas,
            "stale_resource_grants": stale_resource_grants,
            "resource_grants": resource_grants,
        }

    def _pipeline_metrics(self, now: float) -> dict[str, Any]:
        with connect(self.paths.memory) as conn:
            memory_jobs = int(
                conn.execute("SELECT COUNT(*) FROM memory_consolidation_jobs").fetchone()[0]
            )
            memory_pending = int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_consolidation_jobs WHERE status = 'pending'"
                ).fetchone()[0]
            )
            memory_failures = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM memory_consolidation_jobs
                    WHERE (status = 'failed' AND attempts >= 3)
                       OR (status = 'active' AND lease_expires_at <= ?)
                       OR (status = 'pending' AND updated_at <= ?)
                    """,
                    (now, now - 3600),
                ).fetchone()[0]
            )
            identity_aliases = int(conn.execute("SELECT COUNT(*) FROM identity_aliases").fetchone()[0])
        with connect(self.paths.evolution) as conn:
            regressed = int(
                conn.execute(
                    "SELECT COUNT(*) FROM evolution_activations WHERE status = 'regressed'"
                ).fetchone()[0]
            )
            observing = int(
                conn.execute(
                    "SELECT COUNT(*) FROM evolution_activations WHERE status = 'observing'"
                ).fetchone()[0]
            )
        with connect(self.paths.personal_resources) as conn:
            personal_resources = int(
                conn.execute(
                    "SELECT COUNT(*) FROM personal_resources WHERE status != 'deleted'"
                ).fetchone()[0]
            )
        return {
            "memory_jobs": memory_jobs,
            "memory_pending": memory_pending,
            "memory_failures": memory_failures,
            "identity_aliases": identity_aliases,
            "regressed_activations": regressed,
            "observing_activations": observing,
            "personal_resources": personal_resources,
        }


def _zero_slo(name: str, actual: int, *, evidence: dict[str, Any]) -> SLOFact:
    return SLOFact(
        name=name,
        status="met" if actual == 0 else "breached",
        target="= 0",
        actual=float(actual),
        samples=max(1, actual),
        evidence=evidence,
    )


def _ratio_slo(
    name: str,
    actual: float,
    *,
    samples: int,
    minimum_samples: int,
    target: float,
    higher_is_better: bool,
) -> SLOFact:
    if samples < minimum_samples:
        status = "insufficient_data"
    else:
        passed = actual >= target if higher_is_better else actual <= target
        status = "met" if passed else "breached"
    operator = ">=" if higher_is_better else "<="
    return SLOFact(
        name=name,
        status=status,
        target=f"{operator} {target:.2f} with samples >= {minimum_samples}",
        actual=actual,
        samples=samples,
        evidence={"minimum_samples": minimum_samples},
    )
