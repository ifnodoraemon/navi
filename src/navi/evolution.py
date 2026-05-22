from __future__ import annotations

import difflib
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .db import connect
from typing import Any

from .graph import GraphStore
from .tasks import Task, TaskStore
from .trust import TrustRule, TrustStore


@dataclass(frozen=True)
class EvolutionEvent:
    id: str
    task_id: str
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
    source_task_id: str
    status: str
    created_at: float
    applied_at: float
    applied_event_id: str
    eval_cases: str
    evaluation_result: str


@dataclass(frozen=True)
class EvolutionTarget:
    target_type: str
    description: str
    source: str
    permissions_can_expand: bool = False


EVOLUTION_TARGETS: tuple[EvolutionTarget, ...] = (
    EvolutionTarget("prompt_layer", "Versioned prompt layer content that shapes model behavior.", "prompting"),
    EvolutionTarget("skill", "Promptable skill content, metadata, provenance, and trust state.", "skills"),
    EvolutionTarget("memory_item", "Typed durable memory item content and lifecycle state.", "memory"),
    EvolutionTarget("memory_schema", "Memory types, priority, expiry, contradiction, and recall policy.", "memory"),
    EvolutionTarget("tool_spec", "Capability/tool manifest schema, permissions, and descriptions.", "tools", True),
    EvolutionTarget("connector_spec", "Connector affordances, surface commands, and status facts.", "connectors", True),
    EvolutionTarget("trust_rule", "Sender/project scoped autonomy rule.", "trust", True),
    EvolutionTarget("trust_policy", "Autonomy level meanings, promotion, demotion, and approval policy.", "trust", True),
    EvolutionTarget("workflow_policy", "Daemon, execution, approval, and lifecycle decision policy.", "runtime", True),
    EvolutionTarget("eval_case", "Evaluation dataset case and expected behavior.", "evals"),
    EvolutionTarget("graph_node", "Personal graph project, person, and task relationship facts.", "graph"),
    EvolutionTarget("task_execution", "Recorded task execution outcome state.", "execution"),
)


def list_evolution_targets() -> list[dict[str, Any]]:
    return [target.__dict__ for target in EVOLUTION_TARGETS]


def known_evolution_target(target_type: str) -> bool:
    return any(target.target_type == target_type for target in EVOLUTION_TARGETS)


class EvolutionLedger:
    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = home / "evolution.db"
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evolution_events (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evolution_task ON evolution_events(task_id)")
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
                    source_task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    applied_at REAL NOT NULL,
                    applied_event_id TEXT NOT NULL,
                    eval_cases TEXT NOT NULL,
                    evaluation_result TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evolution_proposal_status ON evolution_proposals(status)")

    def record(
        self,
        *,
        task_id: str,
        target_type: str,
        target_id: str,
        reason: str,
        before: str,
        after: str,
    ) -> EvolutionEvent:
        event = EvolutionEvent(
            id=uuid.uuid4().hex,
            task_id=task_id,
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
                    id, task_id, target_type, target_id, reason, before, after,
                    diff, created_at, rolled_back_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.task_id,
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
                SELECT id, task_id, target_type, target_id, reason, before, after,
                       diff, created_at, rolled_back_at
                FROM evolution_events ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [EvolutionEvent(*row) for row in rows]

    def list_for_task(self, task_id: str) -> list[EvolutionEvent]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, task_id, target_type, target_id, reason, before, after,
                       diff, created_at, rolled_back_at
                FROM evolution_events WHERE task_id = ? ORDER BY created_at ASC
                """,
                (task_id,),
            ).fetchall()
        return [EvolutionEvent(*row) for row in rows]

    def get(self, event_id: str) -> EvolutionEvent | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, task_id, target_type, target_id, reason, before, after,
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
        source_task_id: str = "",
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
            source_task_id=source_task_id,
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
                    evidence, source_task_id, status, created_at, applied_at,
                    applied_event_id, eval_cases, evaluation_result
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(proposal.__dict__.values()),
            )
        return proposal

    def list_proposals(self, *, status: str | None = None, limit: int = 100) -> list[EvolutionProposal]:
        with connect(self.db_path) as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT id, target_type, target_id, reason, expected_benefit, risk,
                           before, after, diff, rollback_plan, required_approval_level,
                           evidence, source_task_id, status, created_at, applied_at,
                           applied_event_id, eval_cases, evaluation_result
                    FROM evolution_proposals WHERE status = ? ORDER BY created_at DESC LIMIT ?
                    """,
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, target_type, target_id, reason, expected_benefit, risk,
                           before, after, diff, rollback_plan, required_approval_level,
                           evidence, source_task_id, status, created_at, applied_at,
                           applied_event_id, eval_cases, evaluation_result
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
                       evidence, source_task_id, status, created_at, applied_at,
                       applied_event_id, eval_cases, evaluation_result
                FROM evolution_proposals WHERE id = ?
                """,
                (proposal_id,),
            ).fetchone()
        return EvolutionProposal(*row) if row else None

    def record_proposal_evaluation(self, proposal_id: str, evaluation_result: str) -> EvolutionProposal | None:
        if self.get_proposal(proposal_id) is None:
            return None
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE evolution_proposals SET evaluation_result = ? WHERE id = ?",
                (evaluation_result, proposal_id),
            )
        return self.get_proposal(proposal_id)

    def apply_proposal(self, proposal_id: str) -> EvolutionEvent | None:
        proposal = self.get_proposal(proposal_id)
        if proposal is None:
            return None
        if proposal.status == "applied" and proposal.applied_event_id:
            return self.get(proposal.applied_event_id)
        if proposal.status != "proposed":
            raise ValueError(f"cannot apply proposal in status: {proposal.status}")
        event = self.record(
            task_id=proposal.source_task_id,
            target_type=proposal.target_type,
            target_id=proposal.target_id,
            reason=proposal.reason,
            before=proposal.before,
            after=proposal.after,
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE evolution_proposals
                SET status = ?, applied_at = ?, applied_event_id = ?
                WHERE id = ?
                """,
                ("applied", time.time(), event.id, proposal.id),
            )
        return event

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
        self.trust = TrustStore(home)
        self.tasks = TaskStore(home)

        # Jarvis Memory components
        from .config import load_config
        from .provider import build_provider
        from .memory import MemoryStore

        config = load_config(home)
        self.provider = build_provider(config.model)
        self.memory = MemoryStore(home)

    async def reflect_task(self, task: Task, *, success: bool) -> list[EvolutionEvent]:
        events: list[EvolutionEvent] = []
        reason = "successful task reflection" if success else "failed task reflection"
        events.append(self._update_graph(task, success=success, reason=reason))
        previous_rule = self.trust.get(task.trust_rule_id) if task.trust_rule_id else None
        before = json.dumps(self._trust_payload(previous_rule) if previous_rule else {}, sort_keys=True)
        trust_rule = None
        if previous_rule is not None:
            if success:
                trust_rule = self.trust.record_success(task)
            else:
                trust_rule = await self.trust.record_failure(task)
        if trust_rule:
            after = json.dumps(self._trust_payload(trust_rule), sort_keys=True)
            events.append(
                self.ledger.record(
                    task_id=task.id,
                    target_type="trust_rule",
                    target_id=trust_rule.id,
                    reason=reason,
                    before=before,
                    after=after,
                )
            )
            self.tasks.update_task(
                task.id,
                trust_rule_id=trust_rule.id,
                autonomy_level=trust_rule.autonomy_level,
            )

        # Active Task Learning reflection
        logs = self.tasks.list_execution_logs(task.id)
        await self.memory.extract_memories_from_task(task, logs, self.provider)

        return self.ledger.list_for_task(task.id)

    def apply_proposal(self, proposal_id: str) -> EvolutionEvent | None:
        proposal = self.ledger.get_proposal(proposal_id)
        if proposal is None:
            return None
        if proposal.status == "applied" and proposal.applied_event_id:
            return self.ledger.get(proposal.applied_event_id)
        if proposal.status != "proposed":
            raise ValueError(f"cannot apply proposal in status: {proposal.status}")
        if proposal.target_type == "prompt_layer":
            from .prompting import PromptLayerStore

            PromptLayerStore(self.home).write_override(proposal.target_id, proposal.after)
        return self.ledger.apply_proposal(proposal_id)

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
        elif event.target_type == "trust_rule":
            if event.before and event.before != "{}":
                self.trust.restore(json.loads(event.before))
            else:
                self.trust.delete(event.target_id)
        elif event.target_type == "memory_item":
            if not event.before:
                self.memory.delete_item(event.target_id)
            else:
                self.memory.restore_item(json.loads(event.before))
        elif event.target_type == "task_execution":
            if event.before:
                task_dict = json.loads(event.before)
                self.tasks.update_task(
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
        return self.ledger.mark_rolled_back(event_id)

    def _update_graph(self, task: Task, *, success: bool, reason: str) -> EvolutionEvent:
        name = task.workspace or str(Path.home())
        before_node = self.graph.get_by_name("Project", name)
        before = json.dumps(before_node.data if before_node else {}, sort_keys=True)
        node = self.graph.upsert(
            "Project",
            name,
            {
                "path": name,
                "last_task_id": task.id,
                "last_status": "success" if success else "failure",
                "last_prompt": task.prompt,
            },
        )
        after = json.dumps(node.data, sort_keys=True)
        return self.ledger.record(
            task_id=task.id,
            target_type="graph_node",
            target_id=node.id,
            reason=reason,
            before=before,
            after=after,
        )

    @staticmethod
    def _trust_payload(rule: TrustRule) -> dict[str, Any]:
        return {
            "id": rule.id,
            "name": rule.name,
            "pattern": rule.pattern,
            "project_path": rule.project_path,
            "sender_id": rule.sender_id,
            "autonomy_level": rule.autonomy_level,
            "success_count": rule.success_count,
            "failure_count": rule.failure_count,
            "data": rule.data,
        }
