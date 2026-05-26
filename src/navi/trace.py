from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect


@dataclass(frozen=True)
class TraceEvent:
    id: str
    trace_id: str
    session_id: str
    task_id: str
    phase: str
    source: str
    peer_id: str
    sender_id: str
    tool: str
    model_role: str
    ok: bool
    input_json: str
    output_json: str
    message: str
    created_at: float


@dataclass(frozen=True)
class TraceEvaluation:
    id: str
    trace_id: str
    outcome: str
    failure_domain: str
    recommendation: str
    evidence_json: str
    created_at: float


class TraceStore:
    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = home / "traces.db"
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trace_events (
                    id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    source TEXT NOT NULL,
                    peer_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    model_role TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    input_json TEXT NOT NULL,
                    output_json TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trace_events_trace ON trace_events(trace_id, created_at)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trace_evaluations (
                    id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    failure_domain TEXT NOT NULL,
                    recommendation TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trace_evaluations_trace ON trace_evaluations(trace_id)")

    @staticmethod
    def new_trace_id() -> str:
        return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]

    def add_event(
        self,
        *,
        trace_id: str,
        phase: str,
        session_id: str = "",
        task_id: str = "",
        source: str = "",
        peer_id: str = "",
        sender_id: str = "",
        tool: str = "",
        model_role: str = "",
        ok: bool = True,
        input_data: dict[str, Any] | None = None,
        output_data: dict[str, Any] | None = None,
        message: str = "",
    ) -> TraceEvent:
        event = TraceEvent(
            id=uuid.uuid4().hex,
            trace_id=trace_id,
            session_id=session_id,
            task_id=task_id,
            phase=phase,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            tool=tool,
            model_role=model_role,
            ok=ok,
            input_json=json.dumps(_redact(input_data or {}), ensure_ascii=False, sort_keys=True),
            output_json=json.dumps(_redact(output_data or {}), ensure_ascii=False, sort_keys=True),
            message=message,
            created_at=time.time(),
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO trace_events(
                    id, trace_id, session_id, task_id, phase, source, peer_id,
                    sender_id, tool, model_role, ok, input_json, output_json,
                    message, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.trace_id,
                    event.session_id,
                    event.task_id,
                    event.phase,
                    event.source,
                    event.peer_id,
                    event.sender_id,
                    event.tool,
                    event.model_role,
                    int(event.ok),
                    event.input_json,
                    event.output_json,
                    event.message,
                    event.created_at,
                ),
            )
        return event

    def list_events(self, trace_id: str, *, limit: int = 200) -> list[TraceEvent]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, trace_id, session_id, task_id, phase, source, peer_id,
                       sender_id, tool, model_role, ok, input_json, output_json,
                       message, created_at
                FROM trace_events WHERE trace_id = ? ORDER BY created_at ASC LIMIT ?
                """,
                (trace_id, limit),
            ).fetchall()
        return [TraceEvent(*row[:10], bool(row[10]), *row[11:]) for row in rows]

    def list_trace_ids(self, *, limit: int = 50) -> list[str]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT trace_id FROM trace_events
                GROUP BY trace_id ORDER BY MAX(created_at) DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [row[0] for row in rows]

    def evaluate_trace(self, trace_id: str) -> TraceEvaluation:
        events = self.list_events(trace_id)
        outcome = "success"
        failure_domain = "none"
        recommendation = "No optimization indicated from this trace."
        evidence: dict[str, Any] = {"event_count": len(events)}

        first_failure = next((event for event in events if not event.ok), None)
        if first_failure:
            outcome = "failure"
            evidence["first_failure_phase"] = first_failure.phase
            evidence["first_failure_tool"] = first_failure.tool
            evidence["first_failure_message"] = first_failure.message
            if first_failure.phase == "planner.syscall":
                failure_domain = "prompt_or_provider_parser"
                recommendation = "Review planner prompt, model route, and tool-call parser compatibility."
            elif first_failure.phase == "capability.result":
                failure_domain = "tool_or_capability"
                recommendation = "Review the selected tool spec, arguments, and implementation result."
            elif first_failure.phase == "completion.verify":
                failure_domain = "completion_verifier"
                recovery_plan = next((event for event in events if event.phase == "recovery.plan"), None)
                if recovery_plan:
                    evidence["recovery_plan_recorded"] = True
                    try:
                        recovery_output = json.loads(recovery_plan.output_json or "{}")
                    except json.JSONDecodeError:
                        recovery_output = {}
                    evidence["recovery_recommended"] = recovery_output.get("recommended", "")
                    recommendation = (
                        "Review whether the recorded recovery plan led to a successful follow-up action."
                    )
                else:
                    evidence["recovery_plan_recorded"] = False
                    recommendation = (
                        "Review goal completion criteria and planner follow-up policy; the model tried to finish before "
                        "the observed state satisfied the task."
                    )
            else:
                failure_domain = "runtime"
                recommendation = "Review runtime policy and orchestration around the failing phase."
        elif any(event.phase == "turn.final" and "Step budget limit reached" in event.message for event in events):
            outcome = "degraded"
            failure_domain = "planning_budget"
            recommendation = "Review planner prompt, step budget, and whether tools need more compact observations."
        elif _has_unverified_pending_task_completion(events):
            outcome = "degraded"
            failure_domain = "completion_verifier_gap"
            recommendation = (
                "A tracked task was only recorded or prepared before the turn finished. Tighten completion verification "
                "or extend the agent loop to continue through preparation, approval request, or queueing."
            )
            evidence["pending_task_completion_risk"] = True
        elif not events:
            outcome = "unknown"
            failure_domain = "trace_missing"
            recommendation = "No trace events were recorded; inspect instrumentation."

        return self.record_evaluation(
            trace_id=trace_id,
            outcome=outcome,
            failure_domain=failure_domain,
            recommendation=recommendation,
            evidence=evidence,
        )

    def record_evaluation(
        self,
        *,
        trace_id: str,
        outcome: str,
        failure_domain: str,
        recommendation: str,
        evidence: dict[str, Any] | None = None,
    ) -> TraceEvaluation:
        evaluation = TraceEvaluation(
            id=uuid.uuid4().hex,
            trace_id=trace_id,
            outcome=outcome,
            failure_domain=failure_domain,
            recommendation=recommendation,
            evidence_json=json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
            created_at=time.time(),
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO trace_evaluations(
                    id, trace_id, outcome, failure_domain, recommendation,
                    evidence_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.id,
                    evaluation.trace_id,
                    evaluation.outcome,
                    evaluation.failure_domain,
                    evaluation.recommendation,
                    evaluation.evidence_json,
                    evaluation.created_at,
                ),
            )
        return evaluation


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(token in key_text for token in ("key", "token", "secret", "password", "code")):
                redacted[key] = "[redacted]"
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _has_unverified_pending_task_completion(events: list[TraceEvent]) -> bool:
    if any(event.phase == "completion.verify" for event in events):
        return False
    final_event = next((event for event in reversed(events) if event.phase == "turn.final"), None)
    if final_event is None:
        return False
    for event in events:
        if event.phase != "capability.result" or event.tool != "task.record" or not event.ok:
            continue
        try:
            output = json.loads(event.output_json)
        except json.JSONDecodeError:
            continue
        facts = output.get("facts")
        if not isinstance(facts, dict):
            continue
        status = str(facts.get("status") or "")
        if status in {"pending", "prepared"}:
            return True
    return False
