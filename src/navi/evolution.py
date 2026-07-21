from __future__ import annotations

import difflib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List

from .approval_contract import APPROVAL_DECISION_APPROVE, APPROVAL_STATUS_APPROVED
from .db import connect
from .graph import GraphStore
from .evolution_targets import EvolutionTargetAdapterRegistry
from .paths import db_paths
from .runs import RunStore

logger = logging.getLogger(__name__)


_EVALUATION_RESULTS = frozenset({"approved", "rejected", "pending"})


@dataclass(frozen=True)
class EvolutionEvent:
    id: str
    run_id: str
    target_type: str
    target_id: str
    reason: str
    before: str
    after: str
    diff: str
    created_at: float
    rolled_back_at: float


@dataclass(frozen=True)
class EvolutionProposal:
    id: str
    target_type: str
    target_id: str
    reason: str
    expected_benefit: str
    risk: str
    before: str
    after: str
    diff: str
    rollback_plan: str
    required_approval_level: str
    evidence: str
    source_run_id: str
    status: str
    created_at: float
    applied_at: float
    applied_event_id: str
    eval_cases: str
    evaluation_result: str
    evaluation_evidence: str = ""
    approved_by: str = ""
    approved_at: float = 0.0
    approval_id: str = ""


@dataclass(frozen=True)
class EvolutionTarget:
    target_type: str
    description: str
    source: str
    permissions_can_expand: bool = False


EVOLUTION_TARGETS: tuple[EvolutionTarget, ...] = (
    EvolutionTarget(
        "prompt_layer", "Versioned prompt layer content that shapes model behavior.", "prompting"
    ),
    EvolutionTarget(
        "skill", "Promptable skill content, metadata, provenance, and verification state.", "skills"
    ),
    EvolutionTarget(
        "memory_item", "Typed durable memory item content and lifecycle state.", "memory"
    ),
    EvolutionTarget(
        "eval_case",
        "Evaluation case consumed by the evolution experiment runner.",
        "evals",
    ),
    EvolutionTarget(
        "graph_node", "Personal graph project, person, and task relationship facts.", "graph"
    ),
    EvolutionTarget("run_execution", "Recorded run execution outcome state.", "execution"),
)


def list_evolution_targets() -> list[dict[str, Any]]:
    return [target.__dict__ for target in EVOLUTION_TARGETS]


def known_evolution_target(target_type: str) -> bool:
    return any(target.target_type == target_type for target in EVOLUTION_TARGETS)


# Governance event types recorded in the evolution ledger alongside evolution
# target types. Declaring these prevents schema drift (principle 1.2): the
# ``record()`` gate rejects any ``target_type`` not in this set, so typos and
# undeclared event categories surface loudly instead of silently persisting.
GOVERNANCE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "execution_grant",
        "approval",
    }
)


def known_ledger_target_type(target_type: str) -> bool:
    """Whether ``target_type`` is a declared evolution target or governance event."""
    return known_evolution_target(target_type) or target_type in GOVERNANCE_EVENT_TYPES


class EvolutionLedger:
    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = db_paths(home).evolution
        self._init_db()

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
                    evaluation_result TEXT NOT NULL,
                    evaluation_evidence TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    approved_at REAL NOT NULL,
                    approval_id TEXT NOT NULL
                )
                """
            )
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(evolution_proposals)").fetchall()
            }
            if "approval_id" not in columns:
                conn.execute(
                    "ALTER TABLE evolution_proposals ADD COLUMN approval_id TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_evolution_proposal_status ON evolution_proposals(status)"
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

    def list(self, *, limit: int = 100) -> List[EvolutionEvent]:
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

    def list_for_task(self, run_id: str) -> List[EvolutionEvent]:
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
        eval_cases: List[str] | None = None,
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
            evaluation_evidence="",
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO evolution_proposals(
                    id, target_type, target_id, reason, expected_benefit, risk,
                    before, after, diff, rollback_plan, required_approval_level,
                    evidence, source_run_id, status, created_at, applied_at,
                    applied_event_id, eval_cases, evaluation_result,
                    evaluation_evidence, approved_by, approved_at, approval_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(proposal.__dict__.values()),
            )
        return proposal

    def list_proposals(
        self, *, status: str | None = None, limit: int = 100
    ) -> List[EvolutionProposal]:
        with connect(self.db_path) as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT id, target_type, target_id, reason, expected_benefit, risk,
                           before, after, diff, rollback_plan, required_approval_level,
                           evidence, source_run_id, status, created_at, applied_at,
                           applied_event_id, eval_cases, evaluation_result,
                           evaluation_evidence, approved_by, approved_at, approval_id
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
                           evaluation_evidence, approved_by, approved_at, approval_id
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
                       evaluation_evidence, approved_by, approved_at, approval_id
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
        evaluation_evidence: str = "",
        approval_id: str = "",
    ) -> EvolutionProposal | None:
        """Record model/checker evaluation evidence for an evolution proposal.

        An ``approved`` evaluation requires a durable approval whose arguments
        bind it to the same proposal. The model may propose and evaluate, but it
        cannot self-assign approval.
        """
        proposal = self.get_proposal(proposal_id)
        if proposal is None:
            return None
        evaluation_result = evaluation_result.strip().lower()
        if evaluation_result not in _EVALUATION_RESULTS:
            raise ValueError(
                f"evaluation_result must be one of {sorted(_EVALUATION_RESULTS)}, "
                f"got {evaluation_result!r}"
            )
        evaluation_evidence = evaluation_evidence.strip()
        if evaluation_result == "approved" and not evaluation_evidence:
            raise ValueError("approved evaluation requires evaluation_evidence")
        approver_id = ""
        approved_at = 0.0
        approval_id = approval_id.strip()
        if evaluation_result == "approved":
            approval = self._approved_evolution_apply_approval(
                proposal_id=proposal_id,
                approval_id=approval_id,
            )
            approver_id = approval.resolved_by
            approved_at = approval.updated_at
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE evolution_proposals
                SET evaluation_result = ?, approved_by = ?, approved_at = ?,
                    evaluation_evidence = ?, approval_id = ?
                WHERE id = ?
                """,
                (
                    evaluation_result,
                    approver_id,
                    approved_at,
                    evaluation_evidence,
                    approval_id if evaluation_result == "approved" else "",
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
            after=json.dumps(
                {
                    "evaluation_result": evaluation_result,
                    "evaluation_evidence": evaluation_evidence,
                    "approved_by": approver_id,
                    "approved_at": approved_at,
                    "approval_id": approval_id if evaluation_result == "approved" else "",
                },
                sort_keys=True,
            ),
        )
        return self.get_proposal(proposal_id)

    def _approved_evolution_apply_approval(self, *, proposal_id: str, approval_id: str):
        if not approval_id:
            raise ValueError("approved evaluation requires approval_id")
        approval = RunStore(self.home).get_approval(approval_id)
        if approval is None:
            raise ValueError("approved evaluation approval_id not found")
        if approval.status != APPROVAL_STATUS_APPROVED or approval.decision != APPROVAL_DECISION_APPROVE:
            raise ValueError("approved evaluation approval must be approved")
        if approval.requested_tool != "evolution.apply":
            raise ValueError("approved evaluation approval must target evolution.apply")
        try:
            approved_args = json.loads(approval.args_json or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("approved evaluation approval args_json must be valid JSON") from exc
        if str(approved_args.get("proposal_id") or "") != proposal_id:
            raise ValueError("approved evaluation approval must reference the same proposal_id")
        if not approval.resolved_by:
            raise ValueError("approved evaluation approval must record resolved_by")
        return approval

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
        target = next((t for t in EVOLUTION_TARGETS if t.target_type == proposal.target_type), None)
        if target and target.permissions_can_expand:
            if proposal.required_approval_level not in ("L3", "L4"):
                raise ValueError("permission-expanding proposals require L3/L4 approval level")
        if proposal.evaluation_result != "approved":
            raise ValueError(
                "proposal requires evaluation_result='approved' before it can be applied"
            )
        if not proposal.evaluation_evidence.strip():
            raise ValueError("approved proposal requires evaluation_evidence before apply")
        if not proposal.approval_id.strip():
            raise ValueError("approved proposal requires durable approval_id before apply")

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


class EvolutionEngine:
    def __init__(self, home: Path):
        self.home = home
        self.ledger = EvolutionLedger(home)
        self.graph = GraphStore(home)
        self.runs = RunStore(home)
        self.targets = EvolutionTargetAdapterRegistry(home)

        from .memory import MemoryStore

        self.memory = MemoryStore(home)

    def apply_proposal(self, proposal_id: str) -> EvolutionEvent | None:
        proposal = self.ledger.get_proposal(proposal_id)
        if proposal is None:
            return None
        if proposal.status == "applied" and proposal.applied_event_id:
            return self.ledger.get(proposal.applied_event_id)
        # Reuse the ledger's single authority so engine-side effects and the DB
        # transition share one gate (no drift between the two apply paths).
        self.ledger.assert_proposal_applicable(proposal)
        if json.loads(proposal.eval_cases or "[]"):
            from .evolution_experiments import EvolutionExperimentStore

            EvolutionExperimentStore(self.home).assert_passed(
                proposal.id,
                candidate=proposal.after,
            )

        # Record the ledger event before performing the side effect, so the
        # change is always auditable and rollbackable, even if the file write
        # fails afterwards (principle 7/11).
        event = self.ledger.record_apply_event(proposal)
        try:
            self._write_proposal_side_effect(proposal)
        except Exception as exc:
            # FP-5/L11: the side effect failed after the apply event was
            # recorded. Record a follow-up failure event so the ledger
            # reflects that the change did not land, then surface the error.
            self.ledger.record(
                run_id=proposal.source_run_id,
                target_type=proposal.target_type,
                target_id=proposal.target_id,
                reason="proposal_apply_side_effect_failed",
                before=event.after,
                after=json.dumps(
                    {"error_type": type(exc).__name__, "error": str(exc)}, sort_keys=True
                ),
            )
            raise
        self.ledger.mark_applied(proposal_id, event.id)
        from .evolution_experiments import EvolutionExperimentStore

        EvolutionExperimentStore(self.home).start_activation(
            proposal_id=proposal.id,
            event_id=event.id,
        )
        return event

    def _write_proposal_side_effect(self, proposal: EvolutionProposal) -> None:
        adapter = self.targets.get(proposal.target_type)
        current = adapter.read(proposal.target_id)
        if current != proposal.before:
            raise ValueError(
                "evolution proposal baseline is stale; create a new proposal from current state"
            )
        adapter.validate(proposal.target_id, proposal.after)
        adapter.apply(proposal.target_id, proposal.after)

    def rollback(self, event_id: str) -> EvolutionEvent | None:
        event = self.ledger.get(event_id)
        if event is None:
            return event
        if not event.rolled_back_at:
            if event.target_type == "memory":
                (self.home / "memory" / "MEMORY.md").write_text(event.before, encoding="utf-8")
            else:
                self.targets.get(event.target_type).rollback(event.target_id, event.before)
            event = self.ledger.mark_rolled_back(event_id)
        from .evolution_experiments import EvolutionExperimentStore

        EvolutionExperimentStore(self.home).mark_rolled_back(
            event_id,
            rollback_event_id=event_id,
        )
        return event


def _safe_evolution_target_path(
    home: Path,
    *,
    subdir: str,
    target_id: str,
    suffix: str,
) -> Path:
    name = target_id.strip()
    if (
        not name
        or name in {".", ".."}
        or ".." in name
        or "/" in name
        or "\\" in name
        or Path(name).is_absolute()
    ):
        raise ValueError("evolution target_id must be a single safe name")
    root = (home / subdir).resolve()
    target = (root / f"{name}{suffix}").resolve()
    if target.parent != root:
        raise ValueError("evolution target path escapes its managed directory")
    return target
