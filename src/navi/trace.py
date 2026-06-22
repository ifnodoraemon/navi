from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .db import connect, ensure_schema_version
from .schema import Column, Table, assert_schema_exact


TRACE_STORE_SCHEMA_VERSION = 1


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
        self.db_path = home / "traces.db"
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
    try:
        parsed = json.loads(event.output_json or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _base_trace_evidence(events: list[TraceEvent]) -> dict[str, Any]:
    evidence: dict[str, Any] = {"event_count": len(events)}
    role_events = [event for event in events if event.phase == "agent.role_result"]
    if role_events:
        evidence["agent_role_results"] = [
            {"model_role": event.model_role, "message": event.message} for event in role_events
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
        outcome="success",
        failure_domain="none",
        diagnostic="trace has no failed or degraded rule match",
    )


def _first_failure(events: list[TraceEvent]) -> TraceEvent | None:
    return next((event for event in events if not event.ok), None)


def _record_first_failure_evidence(event: TraceEvent, evidence: dict[str, Any]) -> None:
    evidence["first_failure_phase"] = event.phase
    evidence["first_failure_tool"] = event.tool
    evidence["first_failure_message"] = event.message


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
    return _first_failure_rule(
        events,
        evidence,
        phase="planner.syscall",
        failure_domain="prompt_or_provider_parser",
        diagnostic="first failed event was planner syscall parsing or provider tool selection",
    )


def _safeguard_failure_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    return _first_failure_rule(
        events,
        evidence,
        phase="capability.result",
        failure_domain="safeguard_policy",
        diagnostic="first failed capability result contains a safeguard hook decision",
        predicate=_capability_result_has_safeguard_decision,
    )


def _capability_failure_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    return _first_failure_rule(
        events,
        evidence,
        phase="capability.result",
        failure_domain="tool_or_capability",
        diagnostic="first failed event was a capability result without safeguard decision facts",
    )


def _completion_verifier_failure_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    failure = _first_failure(events)
    if failure is None or failure.phase != "completion.verify":
        return None
    _record_first_failure_evidence(failure, evidence)
    recovery_plan = next((event for event in events if event.phase == "recovery.plan"), None)
    if recovery_plan:
        evidence["recovery_plan_recorded"] = True
        evidence["recovery_recommended"] = _event_output(recovery_plan).get("recommended", "")
        diagnostic = "completion verifier failed after a recovery plan was recorded"
    else:
        evidence["recovery_plan_recorded"] = False
        diagnostic = "completion verifier failed before any recovery plan was recorded"
    return TraceEvaluationDraft("failure", "completion_verifier", diagnostic)


def _runtime_failure_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    failure = _first_failure(events)
    if failure is None:
        return None
    _record_first_failure_evidence(failure, evidence)
    return TraceEvaluationDraft(
        "failure",
        "runtime",
        "first failed event was outside planner, capability, and completion verifier phases",
    )


def _budget_degraded_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    del evidence
    if not any(
        event.phase == "turn.final" and _event_output(event).get("budget_exhausted") is True
        for event in events
    ):
        return None
    return TraceEvaluationDraft(
        "degraded",
        "planning_budget",
        "turn final event reported budget_exhausted=true",
    )


def _planner_no_response_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    if not _planner_call_started_without_result(events):
        return None
    evidence["planner_call_without_result"] = True
    return TraceEvaluationDraft(
        "failure",
        "provider_or_planner_no_response",
        "planner provider call started without planner syscall, planner error, or turn final event",
    )


def _pending_completion_gap_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    if not _has_unverified_pending_run_completion(events):
        return None
    evidence["pending_run_completion_risk"] = True
    return TraceEvaluationDraft(
        "degraded",
        "completion_verifier_gap",
        "turn finished after a delegation run was only pending or prepared",
    )


def _missing_trace_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    del evidence
    if events:
        return None
    return TraceEvaluationDraft(
        "unknown",
        "trace_missing",
        "no trace events were recorded",
    )


TRACE_EVALUATION_RULES: tuple[TraceEvaluationRule, ...] = (
    _planner_failure_rule,
    _safeguard_failure_rule,
    _capability_failure_rule,
    _completion_verifier_failure_rule,
    _runtime_failure_rule,
    _budget_degraded_rule,
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
    return "planner.call.start" in phases and not any(
        phase in {"planner.syscall", "planner.call.error", "turn.final"} for phase in phases
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
    if any(event.phase == "completion.verify" for event in events):
        return False
    final_event = next((event for event in reversed(events) if event.phase == "turn.final"), None)
    if final_event is None:
        return False
    for event in events:
        if event.phase != "capability.result" or not event.ok:
            continue
        facts = _event_output(event).get("facts")
        if not isinstance(facts, dict):
            continue
        entity_type = str(facts.get("entity_type") or "")
        status = str(facts.get("status") or "")
        if entity_type == "delegation_run" and status in {"pending", "prepared"}:
            return True
    return False
