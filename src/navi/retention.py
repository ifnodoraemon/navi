from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect
from .paths import db_paths


@dataclass(frozen=True, slots=True)
class RetentionFacts:
    candidates: int
    compacted: int
    deferred: int
    run_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "compacted": self.compacted,
            "deferred": self.deferred,
            "run_ids": list(self.run_ids),
        }


class DataRetentionManager:
    """Compact expired transient turns while retaining terminal lifecycle facts."""

    def __init__(self, home: Path):
        self.home = home
        self.paths = db_paths(home)
        from .effect_journal import EffectJournal
        from .goals import GoalStore
        from .loop_runs import LoopRunStore
        from .memory import MemoryStore
        from .runs import RunStore
        from .trace import TraceStore

        RunStore(home)
        GoalStore(home)
        LoopRunStore(home)
        EffectJournal(home)
        MemoryStore(home)
        TraceStore(home)

    def compact_expired(self, *, now: float | None = None) -> RetentionFacts:
        current_time = time.time() if now is None else now
        candidates = self._expired_transient_runs(current_time)
        compacted: list[str] = []
        deferred = 0
        for item in candidates:
            lifecycle_run_id = self._lifecycle_run_id(str(item["goal_id"]))
            if not lifecycle_run_id or not self._memory_ready(lifecycle_run_id):
                deferred += 1
                continue
            self._compact_one(item, current_time, lifecycle_run_id=lifecycle_run_id)
            compacted.append(lifecycle_run_id)
        return RetentionFacts(
            candidates=len(candidates),
            compacted=len(compacted),
            deferred=deferred,
            run_ids=tuple(compacted),
        )

    def _expired_transient_runs(self, now: float) -> list[dict[str, Any]]:
        with connect(self.paths.loop_runs) as conn:
            rows = conn.execute(
                """
                SELECT lr.id, lr.goal_id, lr.loop_spec_id, lr.updated_at, ls.spec_json
                FROM loop_runs lr
                JOIN loop_specs ls ON ls.id = lr.loop_spec_id
                WHERE lr.terminal_state != ''
                ORDER BY lr.updated_at ASC
                """
            ).fetchall()
        candidates: list[dict[str, Any]] = []
        for run_id, goal_id, spec_id, updated_at, spec_json in rows:
            try:
                spec = json.loads(str(spec_json))
            except json.JSONDecodeError:
                continue
            goal = spec.get("goal") if isinstance(spec, dict) else None
            metadata = goal.get("metadata") if isinstance(goal, dict) else None
            profile = metadata.get("execution_profile") if isinstance(metadata, dict) else None
            if not isinstance(profile, dict) or profile.get("persistence") != "transient_audit":
                continue
            try:
                retention_seconds = max(0.0, float(profile.get("retention_seconds", 86400)))
            except (TypeError, ValueError):
                retention_seconds = 86400.0
            if float(updated_at) + retention_seconds > now:
                continue
            candidates.append(
                {
                    "run_id": str(run_id),
                    "goal_id": str(goal_id),
                    "spec_id": str(spec_id),
                }
            )
        return candidates

    def _memory_ready(self, run_id: str) -> bool:
        with connect(self.paths.memory) as conn:
            row = conn.execute(
                "SELECT status FROM memory_consolidation_jobs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is not None:
                return str(row[0]) in {"completed", "purged"}
            messages = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return not messages or int(messages[0]) == 0

    def _lifecycle_run_id(self, goal_id: str) -> str:
        with connect(self.paths.goals) as conn:
            row = conn.execute(
                "SELECT run_id FROM goals WHERE id = ?",
                (goal_id,),
            ).fetchone()
        return str(row[0]) if row else ""

    def _compact_one(
        self,
        item: dict[str, str],
        now: float,
        *,
        lifecycle_run_id: str,
    ) -> None:
        loop_run_id = item["run_id"]
        run_id = lifecycle_run_id
        goal_id = item["goal_id"]
        spec_id = item["spec_id"]
        summary = json.dumps(
            {"retention": "summary_only", "compacted_at": now},
            sort_keys=True,
        )
        with connect(self.paths.memory) as conn:
            conn.execute("DELETE FROM messages WHERE run_id = ?", (run_id,))
            conn.execute(
                """
                UPDATE memory_consolidation_jobs
                SET status = 'purged', owner = '', lease_expires_at = 0,
                    error = '', updated_at = ?
                WHERE run_id = ?
                """,
                (now, run_id),
            )
        with connect(self.paths.runs) as conn:
            conn.execute("DELETE FROM tool_call_logs WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM approvals WHERE run_id = ?", (run_id,))
            conn.execute(
                """
                UPDATE runs SET title = '[expired transient turn]', prompt = '',
                    why_now = '', plan_summary = '', result_summary = '[terminal summary retained]',
                    error = '' WHERE id = ?
                """,
                (run_id,),
            )
        with connect(self.paths.goals) as conn:
            conn.execute("DELETE FROM goal_events WHERE goal_id = ?", (goal_id,))
            conn.execute(
                """
                UPDATE goals SET objective = '[expired transient turn]',
                    evidence_json = ?, blocked_reason = '' WHERE id = ?
                """,
                (summary, goal_id),
            )
        with connect(self.paths.loop_runs) as conn:
            conn.execute("DELETE FROM loop_checkpoints WHERE run_id = ?", (loop_run_id,))
            conn.execute("DELETE FROM loop_events WHERE run_id = ?", (loop_run_id,))
            conn.execute("DELETE FROM loop_effects WHERE loop_run_id = ?", (loop_run_id,))
            conn.execute("DELETE FROM loop_specs WHERE id = ?", (spec_id,))
            conn.execute(
                """
                UPDATE loop_runs SET checkpoint_id = '', child_run_ids_json = '[]',
                    locked_resources_json = '[]', evidence_json = ?, version = version + 1
                WHERE id = ?
                """,
                (summary, loop_run_id),
            )
        with connect(self.paths.traces) as conn:
            trace_ids = [
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT trace_id FROM trace_events WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
            ]
            conn.execute("DELETE FROM trace_events WHERE run_id = ?", (run_id,))
            for trace_id in trace_ids:
                conn.execute(
                    "DELETE FROM trace_evaluations WHERE trace_id = ?",
                    (trace_id,),
                )
            self._gc_trace_blobs(conn)

    @staticmethod
    def _gc_trace_blobs(conn: Any) -> None:
        import re

        pattern = re.compile(r'"\$blob"\s*:\s*"([^"]+)"')
        used: set[str] = set()
        for input_json, output_json in conn.execute(
            "SELECT input_json, output_json FROM trace_events"
        ).fetchall():
            used.update(pattern.findall(str(input_json or "")))
            used.update(pattern.findall(str(output_json or "")))
        hashes = {str(row[0]) for row in conn.execute("SELECT hash FROM trace_blobs")}
        for blob_hash in hashes - used:
            conn.execute("DELETE FROM trace_blobs WHERE hash = ?", (blob_hash,))
