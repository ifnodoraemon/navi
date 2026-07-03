"""Evolution ledger persistence."""

from __future__ import annotations

import difflib
import json
import time
import uuid
from pathlib import Path

from ..db import connect
from ..paths import db_paths
from .domain import (
    EVOLUTION_TARGETS,
    EvolutionEvent,
    EvolutionProposal,
    _EVALUATION_RESULTS,
    known_evolution_target,
    known_ledger_target_type,
)


class EvolutionLedger:
    _db_initialized: set[Path] = set()

    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = db_paths(home).evolution
        if self.db_path not in self._db_initialized:
            self._init_db()
            EvolutionLedger._db_initialized.add(self.db_path)

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evolution_events (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    before TEXT NOT NULL,
                    after TEXT NOT NULL,
                    diff TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    rolled_back_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_evolution_task ON evolution_events(run_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evolution_proposals (
                    id TEXT PRIMARY KEY,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    expected_benefit TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    before TEXT NOT NULL,
                    after TEXT NOT NULL,
                    diff TEXT NOT NULL,
                    rollback_plan TEXT NOT NULL,
                    required_approval_level TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    source_run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    applied_at REAL NOT NULL,
                    applied_event_id TEXT NOT NULL,
                    eval_cases TEXT NOT NULL,
                    evaluation_result TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_evolution_proposal_status ON evolution_proposals(status)"
            )
            self._migrate_evolution_proposals(conn)

    @staticmethod
    def _migrate_evolution_proposals(conn) -> None:
        """Backfill approved_by/approved_at on pre-existing evolution.db installs."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(evolution_proposals)")}
        if "approved_by" not in columns:
            conn.execute(
                "ALTER TABLE evolution_proposals ADD COLUMN approved_by TEXT NOT NULL DEFAULT ''"
            )
        if "approved_at" not in columns:
            conn.execute(
                "ALTER TABLE evolution_proposals ADD COLUMN approved_at REAL NOT NULL DEFAULT 0.0"
            )

    def record(
        self,
        *,
        run_id: str,
        target_type: str,
        target_id: str,
        reason: str,
        before: str,
        after: str,
    ) -> EvolutionEvent:
        if not known_ledger_target_type(target_type):
            raise ValueError(f"unknown ledger target type: {target_type}")
        event = EvolutionEvent(
            id=uuid.uuid4().hex,
            run_id=run_id,
            target_type=target_type,
            target_id=target_id,
            reason=reason,
            before=before,
            after=after,
            diff=self._diff(before, after),
            created_at=time.time(),
            rolled_back_at=0.0,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO evolution_events(
                    id, run_id, target_type, target_id, reason, before, after,
                    diff, created_at, rolled_back_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.run_id,
                    event.target_type,
                    event.target_id,
                    event.reason,
                    event.before,
                    event.after,
                    event.diff,
                    event.created_at,
                    event.rolled_back_at,
                ),
            )
        return event

    def list(self, *, limit: int = 100) -> list[EvolutionEvent]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, target_type, target_id, reason, before, after,
                       diff, created_at, rolled_back_at
                FROM evolution_events ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [EvolutionEvent(*row) for row in rows]

    def list_for_task(self, run_id: str) -> list[EvolutionEvent]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, target_type, target_id, reason, before, after,
                       diff, created_at, rolled_back_at
                FROM evolution_events WHERE run_id = ? ORDER BY created_at ASC
                """,
                (run_id,),
            ).fetchall()
        return [EvolutionEvent(*row) for row in rows]

    def get(self, event_id: str) -> EvolutionEvent | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, run_id, target_type, target_id, reason, before, after,
                       diff, created_at, rolled_back_at
                FROM evolution_events WHERE id = ?
                """,
                (event_id,),
            ).fetchone()
        return EvolutionEvent(*row) if row else None

    def mark_rolled_back(self, event_id: str) -> EvolutionEvent | None:
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE evolution_events SET rolled_back_at = ? WHERE id = ?",
                (time.time(), event_id),
            )
        return self.get(event_id)

    def propose(
        self,
        *,
        target_type: str,
        target_id: str,
        reason: str,
        expected_benefit: str,
        risk: str,
        before: str,
        after: str,
        rollback_plan: str,
        required_approval_level: str = "L2",
        evidence: str = "",
        source_run_id: str = "",
        eval_cases: list[str] | None = None,
    ) -> EvolutionProposal:
        if not known_evolution_target(target_type):
            raise ValueError(f"unknown evolution target type: {target_type}")
        for field_name, field_value in (
            ("target_id", target_id),
            ("reason", reason),
            ("expected_benefit", expected_benefit),
            ("rollback_plan", rollback_plan),
        ):
            if not str(field_value).strip():
                raise ValueError(f"proposal {field_name} must not be empty")
        proposal = EvolutionProposal(
            id=uuid.uuid4().hex,
            target_type=target_type,
            target_id=target_id,
            reason=reason,
            expected_benefit=expected_benefit,
            risk=risk,
            before=before,
            after=after,
            diff=self._diff(before, after),
            rollback_plan=rollback_plan,
            required_approval_level=required_approval_level,
            evidence=evidence,
            source_run_id=source_run_id,
            status="proposed",
            created_at=time.time(),
            applied_at=0.0,
            applied_event_id="",
            eval_cases=json.dumps(eval_cases or [], sort_keys=True),
            evaluation_result="",
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO evolution_proposals(
                    id, target_type, target_id, reason, expected_benefit, risk,
                    before, after, diff, rollback_plan, required_approval_level,
                    evidence, source_run_id, status, created_at, applied_at,
                    applied_event_id, eval_cases, evaluation_result,
                    approved_by, approved_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(proposal.__dict__.values()),
            )
        return proposal

    def list_proposals(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[EvolutionProposal]:
        with connect(self.db_path) as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT id, target_type, target_id, reason, expected_benefit, risk,
                           before, after, diff, rollback_plan, required_approval_level,
                           evidence, source_run_id, status, created_at, applied_at,
                           applied_event_id, eval_cases, evaluation_result,
                           approved_by, approved_at
                    FROM evolution_proposals WHERE status = ? ORDER BY created_at DESC LIMIT ?
                    """,
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, target_type, target_id, reason, expected_benefit, risk,
                           before, after, diff, rollback_plan, required_approval_level,
                           evidence, source_run_id, status, created_at, applied_at,
                           applied_event_id, eval_cases, evaluation_result,
                           approved_by, approved_at
                    FROM evolution_proposals ORDER BY created_at DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [EvolutionProposal(*row) for row in rows]

    def get_proposal(self, proposal_id: str) -> EvolutionProposal | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, target_type, target_id, reason, expected_benefit, risk,
                       before, after, diff, rollback_plan, required_approval_level,
                       evidence, source_run_id, status, created_at, applied_at,
                       applied_event_id, eval_cases, evaluation_result,
                       approved_by, approved_at
                FROM evolution_proposals WHERE id = ?
                """,
                (proposal_id,),
            ).fetchone()
        return EvolutionProposal(*row) if row else None

    def record_proposal_evaluation(
        self,
        proposal_id: str,
        evaluation_result: str,
        *,
        approver_id: str = "",
        approved_at: float = 0.0,
    ) -> EvolutionProposal | None:
        """Record who approved an evolution proposal and when.

        Principle 7/8/11: approval source of truth must be inspectable and
        repairable, answering who approved, when, and via which record. An
        ``approved`` result without an ``approver_id`` is rejected so approval
        cannot be self-assigned by the model alone."""
        proposal = self.get_proposal(proposal_id)
        if proposal is None:
            return None
        evaluation_result = evaluation_result.strip().lower()
        if evaluation_result not in _EVALUATION_RESULTS:
            raise ValueError(
                f"evaluation_result must be one of {sorted(_EVALUATION_RESULTS)}, "
                f"got {evaluation_result!r}"
            )
        if evaluation_result == "approved" and not approver_id.strip():
            raise ValueError("approved evaluation requires an approver_id")
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE evolution_proposals
                SET evaluation_result = ?, approved_by = ?, approved_at = ?
                WHERE id = ?
                """,
                (
                    evaluation_result,
                    approver_id.strip(),
                    approved_at,
                    proposal_id,
                ),
            )
        # Emit a ledger event capturing the approval decision (before/after).
        self.record(
            run_id=proposal.source_run_id,
            target_type=proposal.target_type,
            target_id=proposal.target_id,
            reason="proposal_evaluation_recorded",
            before=proposal.evaluation_result,
            after=evaluation_result,
        )
        return self.get_proposal(proposal_id)

    def record_apply_event(self, proposal: EvolutionProposal) -> EvolutionEvent:
        """Record the evolution ledger event capturing before/after/diff.

        Called before the side effect lands so the attempted change is always
        auditable and rollbackable (principle 7/11)."""
        return self.record(
            run_id=proposal.source_run_id,
            target_type=proposal.target_type,
            target_id=proposal.target_id,
            reason=proposal.reason,
            before=proposal.before,
            after=proposal.after,
        )

    def mark_applied(self, proposal_id: str, event_id: str) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE evolution_proposals
                SET status = ?, applied_at = ?, applied_event_id = ?
                WHERE id = ?
                """,
                ("applied", time.time(), event_id, proposal_id),
            )

    @staticmethod
    def assert_proposal_applicable(proposal: EvolutionProposal) -> None:
        """Single authority for whether a proposal may be applied.

        Both the ledger DB transition and the engine side-effects call this so
        the permission-expansion and approval gates cannot drift apart.
        """
        if proposal.status != "proposed":
            raise ValueError(f"cannot apply proposal in status: {proposal.status}")
        target = next(
            (t for t in EVOLUTION_TARGETS if t.target_type == proposal.target_type), None
        )
        if target and target.permissions_can_expand:
            if proposal.required_approval_level not in ("L3", "L4"):
                raise ValueError("permission-expanding proposals require L3/L4 approval level")
        if proposal.evaluation_result != "approved":
            raise ValueError(
                "proposal requires evaluation_result='approved' before it can be applied"
            )

    @staticmethod
    def _diff(before: str, after: str) -> str:
        return "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile="before",
                tofile="after",
            )
        )
