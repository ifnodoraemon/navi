from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .db import connect, ensure_schema_version
from .json_utils import json_object
from .loop import (
    LoopCheckResult,
    LoopDecision,
    LoopDecisionKind,
    LoopDecisionSummary,
    LoopPhase,
    TraceFailureDomain,
    TraceOutcome,
    TracePhase,
    TraceRunStatus,
    TraceRunType,
    TraceRunView,
    classify_loop_blocked,
    classify_loop_failure,
    is_approval_loop_decision,
    loop_decision_ok,
    loop_decision_summary,
)
from .paths import db_paths
from .schema import Column, Table


TRACE_STORE_SCHEMA_VERSION = 1
LOOP_DECISION_PHASE = LoopPhase.DECISION


@dataclass(frozen=True)
class TraceEvent:
    id: str
    trace_id: str
    session_id: str
    run_id: str
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
    diagnostic: str
    evidence_json: str
    created_at: float


@dataclass(frozen=True)
class TraceEvaluationDraft:
    outcome: str
    failure_domain: str
    diagnostic: str


TraceEvaluationRule = Callable[[list[TraceEvent], dict[str, Any]], TraceEvaluationDraft | None]


class TraceStore:
    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = db_paths(home).traces
        self._init_db()

    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            ensure_schema_version(conn, "traces", TRACE_STORE_SCHEMA_VERSION)
            conn.execute(TRACE_EVENTS_TABLE.ddl)
            _ensure_schema_current(conn, TRACE_EVENTS_TABLE)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trace_events_trace ON trace_events(trace_id, created_at)"
            )
            conn.execute(TRACE_EVALUATIONS_TABLE.ddl)
            _ensure_schema_current(conn, TRACE_EVALUATIONS_TABLE)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trace_evaluations_trace ON trace_evaluations(trace_id)"
            )

    @staticmethod
    def new_trace_id() -> str:
        return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]

    def add_event(
        self,
        *,
        trace_id: str,
        phase: str,
        session_id: str = "",
        run_id: str = "",
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
            run_id=run_id,
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
            values: dict[str, Any] = {
                "id": event.id,
                "trace_id": event.trace_id,
                "session_id": event.session_id,
                "run_id": event.run_id,
                "phase": event.phase,
                "source": event.source,
                "peer_id": event.peer_id,
                "sender_id": event.sender_id,
                "tool": event.tool,
                "model_role": event.model_role,
                "ok": int(event.ok),
                "input_json": event.input_json,
                "output_json": event.output_json,
                "message": event.message,
                "created_at": event.created_at,
            }
            conn.execute(
                f"INSERT INTO trace_events({', '.join(_TRACE_EVENT_COLUMNS)}) VALUES ({', '.join('?' for _ in _TRACE_EVENT_COLUMNS)})",
                tuple(values[name] for name in _TRACE_EVENT_COLUMNS),
            )
        return event

    def add_loop_decision(
        self,
        *,
        trace_id: str,
        decision: LoopDecision,
        session_id: str = "",
        run_id: str = "",
        source: str = "",
        peer_id: str = "",
        sender_id: str = "",
    ) -> TraceEvent:
        event_run_id = run_id or decision.run_id
        return self.add_event(
            trace_id=trace_id,
            phase=LOOP_DECISION_PHASE,
            session_id=session_id,
            run_id=event_run_id,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            tool=decision.tool,
            model_role="runtime",
            ok=loop_decision_ok(decision),
            output_data=decision.to_dict(),
            message=f"{decision.decision}: {decision.reason}",
        )

    def list_events(self, trace_id: str, *, limit: int = 200) -> list[TraceEvent]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, trace_id, session_id, run_id, phase, source, peer_id,
                       sender_id, tool, model_role, ok, input_json, output_json,
                       message, created_at
                FROM trace_events WHERE trace_id = ? ORDER BY created_at ASC LIMIT ?
                """,
                (trace_id, limit),
            ).fetchall()
        return [TraceEvent(*row[:10], bool(row[10]), *row[11:]) for row in rows]

    def list_loop_decisions(self, trace_id: str, *, limit: int = 200) -> list[TraceEvent]:
        return [
            event
            for event in self.list_events(trace_id, limit=limit)
            if event.phase == LOOP_DECISION_PHASE
        ]

    def list_run_views(self, trace_id: str, *, limit: int = 200) -> list[TraceRunView]:
        return _trace_run_views(self.list_events(trace_id, limit=limit), trace_id=trace_id)

    def list_events_for_run_or_session(
        self,
        *,
        run_id: str,
        session_id: str = "",
        limit: int = 200,
    ) -> list[TraceEvent]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, trace_id, session_id, run_id, phase, source, peer_id,
                       sender_id, tool, model_role, ok, input_json, output_json,
                       message, created_at
                FROM trace_events
                WHERE run_id = ? OR (? != '' AND session_id = ?)
                ORDER BY created_at ASC LIMIT ?
                """,
                (run_id, session_id, session_id, limit),
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

    def list_evaluations(self, trace_id: str = "", *, limit: int = 50) -> list[TraceEvaluation]:
        if trace_id:
            query = """
                SELECT id, trace_id, outcome, failure_domain, diagnostic, evidence_json, created_at
                FROM trace_evaluations WHERE trace_id = ? ORDER BY created_at DESC LIMIT ?
                """
            params: tuple[Any, ...] = (trace_id, limit)
        else:
            query = """
                SELECT id, trace_id, outcome, failure_domain, diagnostic, evidence_json, created_at
                FROM trace_evaluations ORDER BY created_at DESC LIMIT ?
                """
            params = (limit,)
        with connect(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [TraceEvaluation(*row) for row in rows]

    def evaluate_trace(self, trace_id: str) -> TraceEvaluation:
        events = self.list_events(trace_id)
        evidence = _base_trace_evidence(events)
        draft = _evaluate_trace_with_rules(events, evidence)

        return self.record_evaluation(
            trace_id=trace_id,
            outcome=draft.outcome,
            failure_domain=draft.failure_domain,
            diagnostic=draft.diagnostic,
            evidence=evidence,
        )

    def record_evaluation(
        self,
        *,
        trace_id: str,
        outcome: str,
        failure_domain: str,
        diagnostic: str,
        evidence: dict[str, Any] | None = None,
    ) -> TraceEvaluation:
        evaluation = TraceEvaluation(
            id=uuid.uuid4().hex,
            trace_id=trace_id,
            outcome=outcome,
            failure_domain=failure_domain,
            diagnostic=diagnostic,
            evidence_json=json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
            created_at=time.time(),
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO trace_evaluations(
                    id, trace_id, outcome, failure_domain, diagnostic,
                    evidence_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.id,
                    evaluation.trace_id,
                    evaluation.outcome,
                    evaluation.failure_domain,
                    evaluation.diagnostic,
                    evaluation.evidence_json,
                    evaluation.created_at,
                ),
            )
        return evaluation


def _redact(value: Any) -> Any:
    from .safeguards import redact_secrets

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
    if isinstance(value, str):
        return redact_secrets(value)
    return value


def _event_output(event: TraceEvent) -> dict[str, Any]:
    return json_object(event.output_json)


def _event_input(event: TraceEvent) -> dict[str, Any]:
    return json_object(event.input_json)


def _trace_run_views(events: list[TraceEvent], *, trace_id: str) -> list[TraceRunView]:
    if not events:
        return []
    first_session_id = next((event.session_id for event in events if event.session_id), "")
    root = TraceRunView(
        id=trace_id,
        trace_id=trace_id,
        parent_run_id="",
        name="trace",
        run_type=TraceRunType.CHAIN,
        status=TraceRunStatus.ERROR if any(not event.ok for event in events) else TraceRunStatus.SUCCESS,
        start_time=min(event.created_at for event in events),
        end_time=max(event.created_at for event in events),
        thread_id=first_session_id,
        inputs=_event_input(events[0]),
        outputs=_event_output(events[-1]),
        tags=("navi",),
        metadata={
            "event_count": len(events),
            "source": next((event.source for event in events if event.source), ""),
            "session_id": first_session_id,
        },
    )
    return [root, *(_event_run_view(event, parent_run_id=trace_id) for event in events)]


def _event_run_view(event: TraceEvent, *, parent_run_id: str) -> TraceRunView:
    return TraceRunView(
        id=event.id,
        trace_id=event.trace_id,
        parent_run_id=parent_run_id,
        name=event.tool or event.phase,
        run_type=_event_run_type(event),
        status=TraceRunStatus.SUCCESS if event.ok else TraceRunStatus.ERROR,
        start_time=event.created_at,
        end_time=event.created_at,
        thread_id=event.session_id,
        inputs=_event_input(event),
        outputs=_event_output(event),
        tags=tuple(tag for tag in ("navi", event.phase, event.tool, event.model_role) if tag),
        metadata={
            "phase": event.phase,
            "source": event.source,
            "peer_id": event.peer_id,
            "sender_id": event.sender_id,
            "session_id": event.session_id,
            "run_id": event.run_id,
            "message": event.message,
        },
    )


_EVENT_RUN_TYPES_BY_PHASE: dict[str, TraceRunType] = {
    str(TracePhase.PLANNER_CALL_START): TraceRunType.LLM,
    str(TracePhase.PLANNER_CALL_ERROR): TraceRunType.LLM,
    str(TracePhase.PLANNER_PARSE_ERROR): TraceRunType.LLM,
    str(TracePhase.PLANNER_SYSCALL): TraceRunType.LLM,
    str(TracePhase.CAPABILITY_RESULT): TraceRunType.TOOL,
}


def _event_run_type(event: TraceEvent) -> TraceRunType:
    return _EVENT_RUN_TYPES_BY_PHASE.get(event.phase, TraceRunType.CHAIN)


def _loop_decision_events(events: list[TraceEvent]) -> list[tuple[TraceEvent, dict[str, Any]]]:
    decisions: list[tuple[TraceEvent, dict[str, Any]]] = []
    for event in events:
        if event.phase != LOOP_DECISION_PHASE:
            continue
        output = _event_output(event)
        if output:
            decisions.append((event, output))
    return decisions


def _base_trace_evidence(events: list[TraceEvent]) -> dict[str, Any]:
    evidence: dict[str, Any] = {"event_count": len(events)}
    role_events = [event for event in events if event.phase == TracePhase.AGENT_ROLE_RESULT]
    if role_events:
        evidence["agent_role_results"] = [
            {"model_role": event.model_role, "message": event.message} for event in role_events
        ]
    loop_decisions = _loop_decision_events(events)
    if loop_decisions:
        evidence["loop_decisions"] = [
            _loop_decision_summary(event, output) for event, output in loop_decisions
        ]
    return evidence


def _evaluate_trace_with_rules(
    events: list[TraceEvent],
    evidence: dict[str, Any],
) -> TraceEvaluationDraft:
    for rule in TRACE_EVALUATION_RULES:
        draft = rule(events, evidence)
        if draft is not None:
            return draft
    return TraceEvaluationDraft(
        outcome=TraceOutcome.SUCCESS,
        failure_domain=TraceFailureDomain.NONE,
        diagnostic="trace has no failed or degraded rule match",
    )


def _first_failure(events: list[TraceEvent]) -> TraceEvent | None:
    return next((event for event in events if not event.ok), None)


def _record_first_failure_evidence(event: TraceEvent, evidence: dict[str, Any]) -> None:
    evidence["first_failure_phase"] = event.phase
    evidence["first_failure_tool"] = event.tool
    evidence["first_failure_message"] = event.message


def _loop_decision_summary(event: TraceEvent, output: dict[str, Any]) -> dict[str, Any]:
    return loop_decision_summary(
        output,
        event_tool=event.tool,
        event_run_id=event.run_id,
    ).to_dict()


def _loop_decision_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    decisions = _loop_decision_events(events)
    if not decisions:
        return None

    for event, output in reversed(decisions):
        summary = loop_decision_summary(
            output,
            event_tool=event.tool,
            event_run_id=event.run_id,
        )
        if summary.decision == LoopDecisionKind.FAILED:
            return TraceEvaluationDraft(
                TraceOutcome.FAILURE,
                classify_loop_failure(output),
                _loop_diagnostic(summary, "loop ended with an explicit failure decision"),
            )

    for event, output in reversed(decisions):
        summary = loop_decision_summary(
            output,
            event_tool=event.tool,
            event_run_id=event.run_id,
        )
        if summary.decision == LoopDecisionKind.BLOCKED:
            return TraceEvaluationDraft(
                TraceOutcome.FAILURE,
                classify_loop_blocked(output),
                _loop_diagnostic(summary, "loop was blocked by a checker or gate"),
            )
        if is_approval_loop_decision(output):
            return TraceEvaluationDraft(
                TraceOutcome.DEGRADED,
                TraceFailureDomain.APPROVAL_LOOP,
                _loop_diagnostic(summary, "loop repeated or re-entered an approval gate"),
            )

    for event, output in reversed(decisions):
        summary = loop_decision_summary(
            output,
            event_tool=event.tool,
            event_run_id=event.run_id,
        )
        if summary.decision == LoopDecisionKind.CONVERGED:
            return TraceEvaluationDraft(
                TraceOutcome.DEGRADED,
                TraceFailureDomain.LOOP_NO_PROGRESS,
                _loop_diagnostic(
                    summary,
                    "loop converged after repeated stable progress signature",
                ),
            )

    return None


def _loop_diagnostic(summary: LoopDecisionSummary, fallback: str) -> str:
    if summary.decision and summary.reason:
        return f"{fallback}: {summary.decision} ({summary.reason})"
    if summary.reason:
        return f"{fallback}: {summary.reason}"
    return fallback


def _first_failure_rule(
    events: list[TraceEvent],
    evidence: dict[str, Any],
    *,
    phase: str,
    failure_domain: str,
    diagnostic: str,
    predicate: Callable[[TraceEvent], bool] | None = None,
) -> TraceEvaluationDraft | None:
    failure = _first_failure(events)
    if failure is None or failure.phase != phase:
        return None
    if predicate is not None and not predicate(failure):
        return None
    _record_first_failure_evidence(failure, evidence)
    return TraceEvaluationDraft("failure", failure_domain, diagnostic)


def _planner_failure_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    failure = _first_failure(events)
    if failure is None or failure.phase not in {
        TracePhase.PLANNER_SYSCALL,
        TracePhase.PLANNER_PARSE_ERROR,
    }:
        return None
    _record_first_failure_evidence(failure, evidence)
    return TraceEvaluationDraft(
        TraceOutcome.FAILURE,
        TraceFailureDomain.PLANNER_OR_PARSER,
        "first failed event was planner syscall parsing or provider tool selection",
    )


def _safeguard_failure_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    return _first_failure_rule(
        events,
        evidence,
        phase=TracePhase.CAPABILITY_RESULT,
        failure_domain=TraceFailureDomain.SAFEGUARD_POLICY,
        diagnostic="first failed capability result contains a safeguard hook decision",
        predicate=_capability_result_has_safeguard_decision,
    )


def _capability_failure_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    return _first_failure_rule(
        events,
        evidence,
        phase=TracePhase.CAPABILITY_RESULT,
        failure_domain=TraceFailureDomain.CAPABILITY_FAILURE,
        diagnostic="first failed event was a capability result without safeguard decision facts",
    )


def _checker_failure_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    failure = _first_failure(events)
    if failure is None or failure.phase != LoopPhase.CHECK:
        return None
    _record_first_failure_evidence(failure, evidence)
    recovery_plan = next((event for event in events if event.phase == LoopPhase.RECOVERY), None)
    if recovery_plan:
        evidence["recovery_plan_recorded"] = True
        recovery_output = _event_output(recovery_plan)
        evidence["recovery_blocked"] = bool(recovery_output.get("blocked", True))
        details = recovery_output.get("details")
        if isinstance(details, dict):
            evidence["recovery_detail_keys"] = sorted(details)
        diagnostic = "loop checker failed after a recovery plan was recorded"
    else:
        evidence["recovery_plan_recorded"] = False
        diagnostic = "loop checker failed before any recovery plan was recorded"
    return TraceEvaluationDraft(TraceOutcome.FAILURE, TraceFailureDomain.CHECKER_BLOCKED, diagnostic)


def _runtime_failure_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    failure = _first_failure(events)
    if failure is None:
        return None
    _record_first_failure_evidence(failure, evidence)
    return TraceEvaluationDraft(
        TraceOutcome.FAILURE,
        TraceFailureDomain.RUNTIME,
        "first failed event was outside planner, capability, and loop checker phases",
    )


def _planner_no_response_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    if not _planner_call_started_without_result(events):
        return None
    evidence["planner_call_without_result"] = True
    return TraceEvaluationDraft(
        TraceOutcome.FAILURE,
        TraceFailureDomain.PROVIDER_NO_RESPONSE,
        "planner provider call started without planner syscall, planner error, or turn final event",
    )


def _pending_completion_gap_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    if not _has_unverified_pending_run_completion(events):
        return None
    evidence["pending_run_completion_risk"] = True
    return TraceEvaluationDraft(
        TraceOutcome.DEGRADED,
        TraceFailureDomain.MISSING_COMPLETION_CHECK,
        "turn finished after a delegation run was only pending or prepared",
    )


def _missing_trace_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    del evidence
    if events:
        return None
    return TraceEvaluationDraft(
        TraceOutcome.UNKNOWN,
        TraceFailureDomain.TRACE_MISSING,
        "no trace events were recorded",
    )


TRACE_EVALUATION_RULES: tuple[TraceEvaluationRule, ...] = (
    _loop_decision_rule,
    _planner_failure_rule,
    _safeguard_failure_rule,
    _capability_failure_rule,
    _checker_failure_rule,
    _runtime_failure_rule,
    _planner_no_response_rule,
    _pending_completion_gap_rule,
    _missing_trace_rule,
)


def _capability_result_has_safeguard_decision(event: TraceEvent) -> bool:
    output = _event_output(event)
    facts = output.get("facts")
    return isinstance(facts, dict) and isinstance(facts.get("hook_decision"), dict)


def _planner_call_started_without_result(events: list[TraceEvent]) -> bool:
    phases = [event.phase for event in events]
    return TracePhase.PLANNER_CALL_START in phases and not any(
        phase in {TracePhase.PLANNER_SYSCALL, TracePhase.PLANNER_CALL_ERROR, TracePhase.TURN_FINAL}
        for phase in phases
    )


TRACE_EVENTS_TABLE = Table(
    "trace_events",
    [
        Column("id", "TEXT", primary_key=True),
        Column("trace_id", "TEXT", nullable=False),
        Column("session_id", "TEXT", nullable=False),
        Column("run_id", "TEXT", nullable=False),
        Column("phase", "TEXT", nullable=False),
        Column("source", "TEXT", nullable=False),
        Column("peer_id", "TEXT", nullable=False),
        Column("sender_id", "TEXT", nullable=False),
        Column("tool", "TEXT", nullable=False),
        Column("model_role", "TEXT", nullable=False),
        Column("ok", "INTEGER", nullable=False),
        Column("input_json", "TEXT", nullable=False),
        Column("output_json", "TEXT", nullable=False),
        Column("message", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
    ],
)
TRACE_EVALUATIONS_TABLE = Table(
    "trace_evaluations",
    [
        Column("id", "TEXT", primary_key=True),
        Column("trace_id", "TEXT", nullable=False),
        Column("outcome", "TEXT", nullable=False),
        Column("failure_domain", "TEXT", nullable=False),
        Column("diagnostic", "TEXT", nullable=False),
        Column("evidence_json", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
    ],
)

_TRACE_EVENT_COLUMNS = [col.name for col in TRACE_EVENTS_TABLE.columns]


def _ensure_schema_current(conn, table: Table) -> None:
    """Trace tables are ephemeral audit data: on schema drift we drop and
    recreate them rather than blocking agent startup. This differs from the
    loud ``assert_schema_exact`` used for state-bearing stores (runs/goals)."""
    expected = table.pragma_tuples
    if _table_schema(conn, table.name) == expected:
        return
    conn.execute(f"DROP TABLE IF EXISTS {table.name}")
    conn.execute(table.ddl)
    if _table_schema(conn, table.name) != expected:
        raise RuntimeError(f"{table.name} schema mismatch; expected current Navi schema")


def _table_schema(conn, table: str) -> list[tuple[str, str, int, int]]:
    return [
        (row[1], str(row[2]).upper(), int(row[3]), int(row[5]))
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    ]


def _has_unverified_pending_run_completion(events: list[TraceEvent]) -> bool:
    if any(event.phase == LoopPhase.CHECK for event in events):
        return False
    final_event = next(
        (event for event in reversed(events) if event.phase == TracePhase.TURN_FINAL),
        None,
    )
    if final_event is None:
        return False
    for event in events:
        if event.phase != TracePhase.CAPABILITY_RESULT or not event.ok:
            continue
        facts = _event_output(event).get("facts")
        if not isinstance(facts, dict):
            continue
        entity_type = str(facts.get("entity_type") or "")
        status = str(facts.get("status") or "")
        if entity_type == "delegation_run" and status in {"pending", "prepared"}:
            return True
    return False
