from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .db import connect
from .effect_journal import EffectJournal
from .delivery_outbox import DeliveryOutboxStore
from .evolution import EvolutionLedger
from .evolution_experiments import EvolutionExperimentStore
from .goals import GoalStore
from .identity import IdentityStore
from .lifecycle_saga import LifecycleSagaStore
from .loop_runs import DETACHED_EXECUTION_RECOVERY_GRACE_SECONDS, LoopRunStore
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
    """Stateless projection of durable runtime facts into metrics and SLOs.

    Store construction only ensures current schemas; metric values themselves
    are never persisted as a second source of truth.
    """

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
        DeliveryOutboxStore(home)

    def snapshot(self, *, now: float | None = None, window_seconds: float = 604800) -> SystemMetricsSnapshot:
        current_time = time.time() if now is None else now
        cutoff = current_time - max(60.0, window_seconds)
        runs = self._run_metrics(cutoff)
        traces = self._trace_metrics(cutoff)
        integrity = self._integrity_metrics(current_time)
        pipeline = self._pipeline_metrics(current_time)
        delivery = self._delivery_metrics(cutoff, current_time)
        metrics = (
            MetricFact(
                "task_success_rate",
                runs["success_rate"],
                "ratio",
                runs["evaluated"],
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
                float(integrity["expired_leases"] + integrity["stale_unowned_loops"]),
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
            MetricFact(
                "proactive_delivery_success_rate",
                delivery["proactive_success_rate"],
                "ratio",
                delivery["proactive_terminal"],
                window_seconds,
                "delivery_outbox.db",
            ),
            MetricFact(
                "connector_delivery_overdue_count",
                float(delivery["overdue_pending"]),
                "count",
                delivery["items"],
                window_seconds,
                "delivery_outbox.db",
            ),
        )
        slos = (
            _zero_slo(
                "lifecycle_projection_integrity",
                integrity["orphan_count"] + integrity["pending_sagas"],
                samples=integrity["records"],
                evidence={
                    "orphan_count": integrity["orphan_count"],
                    "pending_sagas": integrity["pending_sagas"],
                },
            ),
            _zero_slo(
                "mutating_effect_certainty",
                integrity["uncertain_effects"],
                samples=integrity["effects"],
                evidence={"uncertain_effects": integrity["uncertain_effects"]},
            ),
            _zero_slo(
                "execution_lease_health",
                integrity["expired_leases"] + integrity["stale_unowned_loops"],
                samples=integrity["active_loops"],
                evidence={
                    "expired_leases": integrity["expired_leases"],
                    "stale_unowned_loops": integrity["stale_unowned_loops"],
                },
            ),
            _zero_slo(
                "resource_release_integrity",
                integrity["stale_resource_grants"],
                samples=integrity["resource_grants"],
                evidence={"stale_resource_grants": integrity["stale_resource_grants"]},
            ),
            _zero_slo(
                "memory_pipeline_health",
                pipeline["memory_failures"],
                samples=pipeline["memory_jobs"],
                evidence={
                    "failed_or_stale_jobs": pipeline["memory_failures"],
                    "pending_jobs": pipeline["memory_pending"],
                },
            ),
            _zero_slo(
                "evolution_activation_safety",
                pipeline["regressed_activations"] + pipeline["uncertain_evolution_applies"],
                samples=pipeline["evolution_apply_attempts"],
                evidence={
                    "regressed": pipeline["regressed_activations"],
                    "observing": pipeline["observing_activations"],
                    "uncertain_applies": pipeline["uncertain_evolution_applies"],
                },
            ),
            _ratio_slo(
                "task_success_rate",
                runs["success_rate"],
                samples=runs["evaluated"],
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
            _ratio_slo(
                "proactive_delivery_success_rate",
                delivery["proactive_success_rate"],
                samples=delivery["proactive_terminal"],
                minimum_samples=5,
                target=0.95,
                higher_is_better=True,
            ),
            _zero_slo(
                "connector_delivery_backlog_health",
                delivery["overdue_pending"],
                samples=delivery["items"],
                evidence={
                    "pending": delivery["pending"],
                    "overdue_pending": delivery["overdue_pending"],
                },
            ),
        )
        overall = "breached" if any(item.status == "breached" for item in slos) else "met"
        if overall == "met" and any(item.status == "insufficient_data" for item in slos):
            overall = "insufficient_data"
        diagnostics = {
            **integrity,
            **pipeline,
            "terminal_runs": runs["terminal"],
            "evaluated_runs": runs["evaluated"],
            "canceled_runs": runs["canceled"],
            "successful_runs": runs["successful"],
            "evaluated_traces": traces["evaluated"],
            "failed_traces": traces["failed"],
            **delivery,
        }
        return SystemMetricsSnapshot(
            generated_at=current_time,
            overall_status=overall,
            metrics=metrics,
            slos=slos,
            diagnostics=diagnostics,
        )

    def _run_metrics(self, cutoff: float) -> dict[str, Any]:
        with connect(self.paths.runs) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*),
                       SUM(CASE WHEN resolution = 'success' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN resolution = 'canceled' THEN 1 ELSE 0 END)
                FROM runs WHERE phase = 'ended' AND updated_at >= ?
                """,
                (cutoff,),
            ).fetchone()
        terminal = int(row[0] or 0) if row else 0
        successful = int(row[1] or 0) if row else 0
        canceled = int(row[2] or 0) if row else 0
        evaluated = terminal - canceled
        return {
            "terminal": terminal,
            "evaluated": evaluated,
            "canceled": canceled,
            "successful": successful,
            "success_rate": successful / evaluated if evaluated else 0.0,
        }

    def _trace_metrics(self, cutoff: float) -> dict[str, Any]:
        with connect(self.paths.traces) as conn:
            row = conn.execute(
                """
                WITH latest AS (
                    SELECT trace_id, outcome, created_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY trace_id ORDER BY created_at DESC, rowid DESC
                           ) AS position
                    FROM trace_evaluations
                )
                SELECT COUNT(*), SUM(CASE WHEN outcome = 'failure' THEN 1 ELSE 0 END)
                FROM latest WHERE position = 1 AND created_at >= ?
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
            run_rows = conn.execute("SELECT id, kind FROM runs").fetchall()
        run_ids = {str(row[0]) for row in run_rows}
        with connect(self.paths.goals) as conn:
            goal_rows = conn.execute("SELECT id, run_id FROM goals").fetchall()
        goal_ids = {str(row[0]) for row in goal_rows}
        goal_run_ids = {str(row[1]) for row in goal_rows}
        missing_runs = [str(row[0]) for row in goal_rows if str(row[1]) not in run_ids]
        missing_goal_runs = [
            str(row[0])
            for row in run_rows
            if str(row[1]).startswith("loop:") and str(row[0]) not in goal_run_ids
        ]
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
            stale_unowned_loops = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM loop_runs
                    WHERE terminal_state = ''
                      AND lease_owner = ''
                      AND updated_at <= ?
                    """,
                    (now - DETACHED_EXECUTION_RECOVERY_GRACE_SECONDS,),
                ).fetchone()[0]
            )
            active_loops = int(
                conn.execute(
                    "SELECT COUNT(*) FROM loop_runs WHERE terminal_state = ''"
                ).fetchone()[0]
            )
            uncertain_effects = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM loop_effects
                    WHERE status = 'uncertain'
                       OR (status = 'active' AND lease_expires_at <= ?)
                    """,
                    (now,),
                ).fetchone()[0]
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
            "orphan_count": len(missing_runs) + len(missing_goals) + len(missing_goal_runs),
            "orphan_goal_ids": missing_runs[:20],
            "orphan_loop_run_ids": missing_goals[:20],
            "orphan_run_ids": missing_goal_runs[:20],
            "active_loops": active_loops,
            "expired_leases": expired_leases,
            "stale_unowned_loops": stale_unowned_loops,
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
                    WHERE status = 'dead_letter'
                       OR (status = 'failed' AND attempts >= 3)
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
            evolution_activations = int(
                conn.execute("SELECT COUNT(*) FROM evolution_activations").fetchone()[0]
            )
            uncertain_evolution_applies = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM evolution_proposals
                    WHERE status IN ('applying', 'apply_uncertain')
                    """
                ).fetchone()[0]
            )
            evolution_apply_attempts = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM evolution_proposals
                    WHERE status IN ('applying', 'apply_uncertain', 'applied', 'rolled_back')
                    """
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
            "evolution_activations": evolution_activations,
            "uncertain_evolution_applies": uncertain_evolution_applies,
            "evolution_apply_attempts": evolution_apply_attempts,
            "personal_resources": personal_resources,
        }

    def _delivery_metrics(self, cutoff: float, now: float) -> dict[str, Any]:
        with connect(self.paths.delivery_outbox) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*),
                       SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END),
                       SUM(
                           CASE
                               WHEN status = 'pending' AND next_attempt_at <= ? THEN 1
                               ELSE 0
                           END
                       ),
                       SUM(
                           CASE
                               WHEN body_provenance != 'response_ready'
                                AND status IN ('sent', 'failed') THEN 1
                               ELSE 0
                           END
                       ),
                       SUM(
                           CASE
                               WHEN body_provenance != 'response_ready' AND status = 'sent'
                               THEN 1
                               ELSE 0
                           END
                       )
                FROM delivery_outbox WHERE created_at >= ?
                """,
                (now - 300.0, cutoff),
            ).fetchone()
        items = int(row[0] or 0) if row else 0
        pending = int(row[1] or 0) if row else 0
        overdue_pending = int(row[2] or 0) if row else 0
        proactive_terminal = int(row[3] or 0) if row else 0
        proactive_sent = int(row[4] or 0) if row else 0
        return {
            "delivery_items": items,
            "items": items,
            "pending": pending,
            "overdue_pending": overdue_pending,
            "proactive_terminal": proactive_terminal,
            "proactive_sent": proactive_sent,
            "proactive_success_rate": (
                proactive_sent / proactive_terminal if proactive_terminal else 0.0
            ),
        }


def _zero_slo(
    name: str,
    actual: int,
    *,
    samples: int,
    evidence: dict[str, Any],
) -> SLOFact:
    return SLOFact(
        name=name,
        status=(
            "insufficient_data"
            if samples == 0
            else ("met" if actual == 0 else "breached")
        ),
        target="= 0",
        actual=float(actual),
        samples=samples,
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
