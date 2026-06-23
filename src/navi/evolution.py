from __future__ import annotations

import difflib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .db import connect
from .paths import db_paths
from typing import Any

from .graph import GraphStore
from .runs import Run, RunStore

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
    approved_by: str = ""
    approved_at: float = 0.0


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
        "memory_schema",
        "Memory types, priority, expiry, contradiction, and recall policy.",
        "memory",
    ),
    EvolutionTarget(
        "tool_spec",
        "Capability/tool manifest schema, permissions, and descriptions.",
        "tools",
        True,
    ),
    EvolutionTarget(
        "connector_spec",
        "Connector affordances, surface commands, and status facts.",
        "connectors",
        True,
    ),
    EvolutionTarget(
        "workflow_policy",
        "Daemon, execution, approval, and lifecycle decision policy.",
        "runtime",
        True,
    ),
    EvolutionTarget("eval_case", "Evaluation dataset case and expected behavior.", "evals"),
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
    return (
        known_evolution_target(target_type)
        or target_type in GOVERNANCE_EVENT_TYPES
    )


# Data-driven map of which spec-file targets persist to (subdir, suffix) on apply.
# prompt_layer is handled separately because it writes through PromptLayerStore.
_SPEC_FILE_TARGETS: dict[str, tuple[str, str]] = {
    "tool_spec": ("specs", ".yaml"),
    "connector_spec": ("specs", ".yaml"),
    "workflow_policy": ("specs", ".yaml"),
    "memory_schema": ("specs", ".yaml"),
    "eval_case": ("evals", ".json"),
}


def _summarize_trace_events(events: list[Any]) -> str:
    lines: list[str] = []
    for event in events:
        if event.phase == "turn.start":
            message = _json_field(event.input_json, "message")
            if message:
                lines.append(f"user: {message}")
        elif event.phase == "planner.syscall":
            details = _json_object(event.output_json)
            tool = str(details.get("tool") or event.tool or "").strip()
            reason = str(details.get("reason") or event.message or "").strip()
            if tool:
                lines.append(f"planner selected {tool}: {reason}")
        elif event.phase == "capability.result":
            outcome = "ok" if event.ok else "failed"
            lines.append(f"capability {event.tool} {outcome}: {event.message}".strip())
        elif event.phase == "turn.final":
            lines.append(f"assistant: {event.message}")
    return "\n".join(line for line in lines if line)[:12000]


def _json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _json_field(raw: str, field: str) -> str:
    value = _json_object(raw).get(field)
    return str(value).strip() if value is not None else ""


def _daily_journey_eval_schema() -> dict[str, Any]:
    return {
        "name": "daily_journey_eval",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "id": {"type": "string"},
                "user_goal": {"type": "string"},
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "user": {"type": "string"},
                            "expect": {
                                "type": "object",
                                "additionalProperties": True,
                            },
                        },
                        "required": ["user", "expect"],
                    },
                },
            },
            "required": ["id", "user_goal", "steps"],
        },
    }


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
            reason=f"evaluation recorded: {evaluation_result}",
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


class EvolutionEngine:
    def __init__(self, home: Path):
        self.home = home
        self.ledger = EvolutionLedger(home)
        self.graph = GraphStore(home)
        self.runs = RunStore(home)

        # Jarvis Memory components
        from .config import load_config
        from .provider import build_provider
        from .memory import MemoryStore

        config = load_config(home)
        self.provider = build_provider(config.model)
        self.memory = MemoryStore(home)

    async def reflect_run(self, task: Run, *, success: bool) -> list[EvolutionEvent]:
        events: list[EvolutionEvent] = []
        reason = "successful task reflection" if success else "failed task reflection"
        events.append(self._update_graph(task, success=success, reason=reason))

        # Active Run Learning reflection
        logs = self.runs.list_execution_logs(task.id)
        await self.memory.extract_memories_from_run(task, logs, self.provider)

        return self.ledger.list_for_task(task.id)

    async def extract_evals_from_session(self, session_id: str, *, run_id: str = "") -> None:
        from navi.evals import load_daily_journey_eval_dataset
        from navi.provider import ChatMessage
        from navi.trace import TraceStore
        import yaml

        events = TraceStore(self.home).list_events_for_run_or_session(
            run_id=run_id,
            session_id=session_id,
            limit=200,
        )
        if not events:
            return

        trace_summary = _summarize_trace_events(events)
        if not trace_summary:
            return

        prompt = (
            "Extract one daily user journey eval from these Navi trace facts. "
            "Use only facts present in the trace. Do not invent hidden state.\n\n"
            f"{trace_summary}"
        )
        response = await self.provider.complete_for(
            "planner",
            [ChatMessage(role="user", content=prompt)],
            output_schema=_daily_journey_eval_schema(),
        )
        if not response.strip():
            return

        extracted = json.loads(response)
        if not isinstance(extracted, dict) or not extracted.get("steps"):
            return

        evals_path = self.home.parent / "evals" / "auto_captured_journeys.yaml"
        before = evals_path.read_text(encoding="utf-8") if evals_path.exists() else ""
        if evals_path.exists():
            data = load_daily_journey_eval_dataset(evals_path)
        else:
            data = {"version": 1, "journeys": []}
        data["journeys"].append(extracted)
        after = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        # FP-5: record the ledger event BEFORE the file side effect lands, so a
        # crash between the two never leaves an unaudited change. Mirrors the
        # apply_proposal ledger-before-side-effect ordering.
        self.ledger.record(
            run_id=run_id,
            target_type="eval_case",
            target_id=str(extracted.get("id") or session_id),
            reason=f"auto-captured journey eval from session {session_id}",
            before=before,
            after=after,
        )
        evals_path.parent.mkdir(parents=True, exist_ok=True)
        evals_path.write_text(after, encoding="utf-8")
        logger.info("extracted daily eval from session %s run %s", session_id, run_id)

    def apply_proposal(self, proposal_id: str) -> EvolutionEvent | None:
        proposal = self.ledger.get_proposal(proposal_id)
        if proposal is None:
            return None
        if proposal.status == "applied" and proposal.applied_event_id:
            return self.ledger.get(proposal.applied_event_id)
        # Reuse the ledger's single authority so engine-side effects and the DB
        # transition share one gate (no drift between the two apply paths).
        self.ledger.assert_proposal_applicable(proposal)

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
                reason=f"apply side-effect failed: {exc}",
                before=event.after,
                after="",
            )
            raise
        self.ledger.mark_applied(proposal_id, event.id)
        return event

    def _write_proposal_side_effect(self, proposal: EvolutionProposal) -> None:
        if proposal.target_type == "prompt_layer":
            from .prompting import PromptLayerStore

            PromptLayerStore(self.home).write_override(proposal.target_id, proposal.after)
            return
        spec_target = _SPEC_FILE_TARGETS.get(proposal.target_type)
        if spec_target is None:
            # Declared evolution targets that have no apply side effect must
            # fail loudly rather than recording a no-op event (principle 1.2).
            raise ValueError(
                f"proposal apply has no side-effect handler for "
                f"target_type={proposal.target_type}"
            )
        subdir, suffix = spec_target
        spec_path = self.home / subdir / f"{proposal.target_id}{suffix}"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(proposal.after, encoding="utf-8")

    def rollback(self, event_id: str) -> EvolutionEvent | None:
        event = self.ledger.get(event_id)
        if event is None or event.rolled_back_at:
            return event
        if event.target_type == "memory":
            (self.home / "memory" / "MEMORY.md").write_text(event.before, encoding="utf-8")
        elif event.target_type == "skill":
            path = Path(event.target_id)
            skills_dir = (self.home / "skills").resolve().absolute()
            try:
                resolved_path = path.resolve().absolute()
                is_safe = skills_dir == resolved_path or skills_dir in resolved_path.parents
            except Exception:
                is_safe = False
            if not is_safe:
                raise ValueError("Skill path must be within the home skills directory")

            if event.before:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(event.before, encoding="utf-8")
            elif path.exists():
                path.unlink()
        elif event.target_type == "graph_node":
            if event.before and event.before != "{}":
                self.graph.replace_data(event.target_id, json.loads(event.before))
            else:
                self.graph.delete(event.target_id)

        elif event.target_type == "memory_item":
            if not event.before:
                self.memory.delete_item(event.target_id)
            else:
                self.memory.restore_item(json.loads(event.before))
                # FP-5: a rolled-back evolution reduces confidence in the
                # affected memory item, since the change it represented was
                # rejected and should not retain its original trust level.
                self.memory.reduce_confidence(event.target_id, delta=0.2)
        elif event.target_type == "run_execution":
            if event.before:
                task_dict = json.loads(event.before)
                self.runs.update_run(
                    event.target_id,
                    status=task_dict.get("status", "queued"),
                    result_summary=task_dict.get("result_summary", ""),
                    error=task_dict.get("error", ""),
                )
        elif event.target_type == "prompt_layer":
            from .prompting import PromptLayerStore

            store = PromptLayerStore(self.home)
            if event.before:
                store.write_override(event.target_id, event.before)
            else:
                store.delete_override(event.target_id)
        elif event.target_type in (
            "tool_spec",
            "connector_spec",
            "workflow_policy",
            "memory_schema",
            "eval_case",
        ):
            ext = "json" if event.target_type == "eval_case" else "yaml"
            folder = "evals" if event.target_type == "eval_case" else "specs"
            spec_path = self.home / folder / f"{event.target_id}.{ext}"
            if event.before:
                spec_path.parent.mkdir(parents=True, exist_ok=True)
                spec_path.write_text(event.before, encoding="utf-8")
            elif spec_path.exists():
                spec_path.unlink()
        return self.ledger.mark_rolled_back(event_id)

    def _update_graph(self, task: Run, *, success: bool, reason: str) -> EvolutionEvent:
        name = task.workspace.strip()
        if not name:
            raise ValueError(f"Run {task.id} has no workspace")
        before_node = self.graph.get_by_name("Project", name)
        before = json.dumps(before_node.data if before_node else {}, sort_keys=True)
        node = self.graph.upsert(
            "Project",
            name,
            {
                "path": name,
                "last_run_id": task.id,
                "last_status": "success" if success else "failure",
                "last_prompt": task.prompt,
            },
        )
        after = json.dumps(node.data, sort_keys=True)
        return self.ledger.record(
            run_id=task.id,
            target_type="graph_node",
            target_id=node.id,
            reason=reason,
            before=before,
            after=after,
        )
