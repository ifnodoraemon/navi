from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect
from .goals import Goal, GoalStore
from .paths import db_paths
from .runs import Run, RunStore


@dataclass(frozen=True)
class LifecycleSaga:
    id: str
    operation_key: str
    run_id: str
    goal_id: str
    payload_json: str
    status: str
    attempts: int
    last_error: str
    created_at: float
    updated_at: float
    completed_at: float


class LifecycleSagaStore:
    """Recoverable projection of one LoopRun result into Run and Goal stores."""

    def __init__(self, home: Path):
        self.home = home
        self.db_path = db_paths(home).loop_runs
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_sagas (
                    id TEXT PRIMARY KEY,
                    operation_key TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    goal_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    last_error TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_sagas_pending "
                "ON lifecycle_sagas(status, updated_at)"
            )

    def prepare(
        self,
        *,
        operation_key: str,
        run_id: str,
        goal_id: str,
        run_updates: dict[str, Any],
        goal_evidence: dict[str, Any],
    ) -> LifecycleSaga:
        now = time.time()
        payload = json.dumps(
            {"run_updates": run_updates, "goal_evidence": goal_evidence},
            ensure_ascii=False,
            sort_keys=True,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO lifecycle_sagas(
                    id, operation_key, run_id, goal_id, payload_json, status,
                    attempts, last_error, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0, '', ?, ?, 0)
                ON CONFLICT(operation_key) DO NOTHING
                """,
                (uuid.uuid4().hex, operation_key, run_id, goal_id, payload, now, now),
            )
            row = conn.execute(
                """
                SELECT id, operation_key, run_id, goal_id, payload_json, status,
                       attempts, last_error, created_at, updated_at, completed_at
                FROM lifecycle_sagas WHERE operation_key = ?
                """,
                (operation_key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("lifecycle saga was not persisted")
        return LifecycleSaga(*row)

    def apply(self, saga: LifecycleSaga) -> tuple[Run, Goal]:
        payload = _json_dict(saga.payload_json)
        run_updates = payload.get("run_updates")
        goal_evidence = payload.get("goal_evidence")
        if not isinstance(run_updates, dict) or not isinstance(goal_evidence, dict):
            raise ValueError("lifecycle saga payload is invalid")
        try:
            run = RunStore(self.home).update_run(saga.run_id, **run_updates)
            if run is None:
                raise KeyError(f"run not found: {saga.run_id}")
            goal = GoalStore(self.home).update_for_run(run, evidence=goal_evidence)
            if goal is None or goal.id != saga.goal_id:
                raise KeyError(f"goal not found for run: {saga.run_id}")
        except Exception as exc:
            self._mark_failed(saga.id, exc)
            raise
        self._mark_completed(saga.id)
        return run, goal

    def recover_pending(self, *, limit: int = 100) -> list[str]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, operation_key, run_id, goal_id, payload_json, status,
                       attempts, last_error, created_at, updated_at, completed_at
                FROM lifecycle_sagas
                WHERE status IN ('pending', 'failed')
                ORDER BY updated_at ASC LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        recovered: list[str] = []
        for row in rows:
            saga = LifecycleSaga(*row)
            try:
                self.apply(saga)
            except Exception:
                continue
            recovered.append(saga.id)
        return recovered

    def recover_open_orphans(
        self,
        *,
        now: float | None = None,
        grace_seconds: float = 60.0,
    ) -> dict[str, list[str]]:
        """Fail stale partial loop creations without racing an in-flight creator."""
        from .lifecycle import Acceptance, Governance, Phase, Resolution

        current_time = time.time() if now is None else now
        cutoff = current_time - max(1.0, grace_seconds)
        runs = RunStore(self.home)
        goals = GoalStore(self.home)
        run_rows = runs.list(limit=100000)
        goal_rows = goals.list(limit=100000)
        run_ids = {run.id for run in run_rows}
        goal_run_ids = {goal.run_id for goal in goal_rows}
        goal_ids = {goal.id for goal in goal_rows}
        recovered_runs: list[str] = []
        recovered_goals: list[str] = []
        recovered_loops: list[str] = []

        for run in run_rows:
            if (
                not run.kind.startswith("loop:")
                or run.id in goal_run_ids
                or run.phase == Phase.ENDED
                or run.created_at > cutoff
            ):
                continue
            runs.update_run(
                run.id,
                phase=Phase.ENDED,
                governance=Governance.NONE,
                acceptance=Acceptance.REJECTED,
                resolution=Resolution.FAILED,
                result_summary="cross_store_open_recovered",
                error="goal projection missing after loop creation grace period",
            )
            recovered_runs.append(run.id)

        with connect(self.db_path) as conn:
            loop_rows = conn.execute(
                "SELECT id, goal_id, created_at, terminal_state FROM loop_runs"
            ).fetchall()
        loop_goal_ids = {str(row[1]) for row in loop_rows}
        for goal in goal_rows:
            if (
                goal.id in loop_goal_ids
                or goal.phase == Phase.ENDED
                or goal.created_at > cutoff
            ):
                continue
            candidate_run = runs.get(goal.run_id) if goal.run_id in run_ids else None
            if candidate_run is not None and candidate_run.phase != Phase.ENDED:
                runs.update_run(
                    candidate_run.id,
                    phase=Phase.ENDED,
                    governance=Governance.NONE,
                    acceptance=Acceptance.REJECTED,
                    resolution=Resolution.FAILED,
                    result_summary="cross_store_open_recovered",
                    error="loop run missing after creation grace period",
                )
            goals.update_state(
                goal.id,
                phase=Phase.ENDED,
                governance=Governance.NONE,
                acceptance=Acceptance.REJECTED,
                resolution=Resolution.FAILED,
                blocked_reason="loop run missing after creation grace period",
                task_status="archived",
                evidence={"recovery": "cross_store_open_orphan"},
                event_type="goal.open_orphan_recovered",
            )
            recovered_goals.append(goal.id)

        with connect(self.db_path) as conn:
            for loop_run_id, goal_id, created_at, terminal_state in loop_rows:
                if (
                    str(goal_id) in goal_ids
                    or str(terminal_state)
                    or float(created_at) > cutoff
                ):
                    continue
                evidence = json.dumps(
                    {
                        "recovery": "cross_store_open_orphan",
                        "reason": "goal missing after creation grace period",
                    },
                    sort_keys=True,
                )
                conn.execute(
                    """
                    UPDATE loop_runs
                    SET terminal_state = 'failed', evidence_json = ?, version = version + 1,
                        lease_owner = '', lease_expires_at = 0, updated_at = ?
                    WHERE id = ? AND terminal_state = ''
                    """,
                    (evidence, current_time, str(loop_run_id)),
                )
                recovered_loops.append(str(loop_run_id))
        return {
            "runs": recovered_runs,
            "goals": recovered_goals,
            "loop_runs": recovered_loops,
        }

    def get(self, saga_id: str) -> LifecycleSaga | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, operation_key, run_id, goal_id, payload_json, status,
                       attempts, last_error, created_at, updated_at, completed_at
                FROM lifecycle_sagas WHERE id = ?
                """,
                (saga_id,),
            ).fetchone()
        return LifecycleSaga(*row) if row else None

    def _mark_failed(self, saga_id: str, error: Exception) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE lifecycle_sagas
                SET status = 'failed', attempts = attempts + 1, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (f"{type(error).__name__}: {error}", time.time(), saga_id),
            )

    def _mark_completed(self, saga_id: str) -> None:
        now = time.time()
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE lifecycle_sagas
                SET status = 'completed', attempts = attempts + 1, last_error = '',
                    updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (now, now, saga_id),
            )


def _json_dict(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
