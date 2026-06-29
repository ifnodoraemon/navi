from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import hashlib
from .capability_contract import CAPABILITY_ERROR_REASON_KEY
from .completion_checks import completion_block_reason, delegation_event_incomplete
from .db import connect, ensure_schema_version
from .json_utils import json_object
from .loop import (
    LoopCheckName,
    LoopCheckResult,  # noqa: F401 - re-exported for trace tests and callers.
    LoopDecision,
    LoopDecisionKind,
    LoopDecisionSummary,
    LoopPhase,
    LoopReason,
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
    evidence_json: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "trace_id": self.trace_id,
            "outcome": self.outcome,
            "failure_domain": self.failure_domain,
            "evidence": json_object(self.evidence_json),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class TraceEvaluationDraft:
    outcome: str
    failure_domain: str


TraceEvaluationRule = Callable[[list[TraceEvent], dict[str, Any]], TraceEvaluationDraft | None]
LoopDecisionEvaluationRule = Callable[
    [LoopDecisionSummary, dict[str, Any], list[TraceEvent], dict[str, Any]],
    TraceEvaluationDraft | None,
]


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
            conn.execute(TRACE_BLOBS_TABLE.ddl)
            _ensure_schema_current(conn, TRACE_BLOBS_TABLE)
        self.clean_old_traces()

    def clean_old_traces(self, days: int = 30) -> None:
        """Deletes traces older than the specified number of days to prevent DB bloat."""
        cutoff = __import__("time").time() - (days * 24 * 3600)
        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM trace_events WHERE created_at < ?", (cutoff,))
            # We don't clean trace_blobs here since multiple events might reference them, 
            # or we could delete orphaned blobs later if needed.

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
        blobs_to_insert = {}
        def _insert_blob(h: str, c: str) -> None:
            blobs_to_insert[h] = c

        input_data_extracted = _extract_blobs(_redact(input_data or {}), _insert_blob)
        output_data_extracted = _extract_blobs(_redact(output_data or {}), _insert_blob)

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
            input_json=json.dumps(input_data_extracted, ensure_ascii=False, sort_keys=True),
            output_json=json.dumps(output_data_extracted, ensure_ascii=False, sort_keys=True),
            message=message,
            created_at=time.time(),
        )
        with connect(self.db_path) as conn:
            for h, c in blobs_to_insert.items():
                conn.execute("INSERT OR IGNORE INTO trace_blobs(hash, content) VALUES (?, ?)", (h, c))
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
        return replace(
            event,
            input_json=json.dumps(_redact(input_data or {}), ensure_ascii=False, sort_keys=True),
            output_json=json.dumps(_redact(output_data or {}), ensure_ascii=False, sort_keys=True),
        )

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
        events = [TraceEvent(*row[:10], bool(row[10]), *row[11:]) for row in rows]
        return self._resolve_events_blobs(events)

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
        events = [TraceEvent(*row[:10], bool(row[10]), *row[11:]) for row in rows]
        return self._resolve_events_blobs(events)

    def list_trace_ids(self, *, limit: int = 50, offset: int = 0, has_error: bool | None = None) -> list[str]:
        query = "SELECT trace_id FROM trace_events GROUP BY trace_id"
        if has_error is True:
            query += " HAVING MIN(ok) = 0"
        elif has_error is False:
            query += " HAVING MIN(ok) != 0"
        
        query += " ORDER BY MAX(created_at) DESC LIMIT ? OFFSET ?"
        with connect(self.db_path) as conn:
            rows = conn.execute(query, (limit, offset)).fetchall()
        return [row[0] for row in rows]

    def _fetch_blobs(self, hashes: set[str]) -> dict[str, str]:
        if not hashes:
            return {}
        placeholders = ",".join("?" for _ in hashes)
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT hash, content FROM trace_blobs WHERE hash IN ({placeholders})",
                tuple(hashes),
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def _resolve_events_blobs(self, events: list[TraceEvent]) -> list[TraceEvent]:
        hashes = set()
        parsed_data = []
        for e in events:
            if e.input_json and '"$blob"' in e.input_json or e.output_json and '"$blob"' in e.output_json:
                data = {
                    "in": json.loads(e.input_json) if e.input_json else None,
                    "out": json.loads(e.output_json) if e.output_json else None,
                }
                parsed_data.append((e, data))

                def _find_hashes(d: Any) -> None:
                    if isinstance(d, dict):
                        if len(d) == 1 and "$blob" in d:
                            hashes.add(d["$blob"])
                        else:
                            for v in d.values():
                                _find_hashes(v)
                    elif isinstance(d, list):
                        for v in d:
                            _find_hashes(v)

                _find_hashes(data)
            else:
                parsed_data.append((e, None))

        if not hashes:
            return events

        blob_map = self._fetch_blobs(hashes)

        def _replace(d: Any) -> Any:
            if isinstance(d, dict):
                if len(d) == 1 and "$blob" in d:
                    h = d["$blob"]
                    return blob_map.get(h, f"<missing blob: {h}>")
                return {k: _replace(v) for k, v in d.items()}
            if isinstance(d, list):
                return [_replace(v) for v in d]
            return d

        resolved_events = []
        for e, data in parsed_data:
            if data is None:
                resolved_events.append(e)
            else:
                resolved_data = _replace(data)
                resolved_events.append(
                    replace(
                        e,
                        input_json=json.dumps(resolved_data["in"], ensure_ascii=False, sort_keys=True)
                        if resolved_data["in"] is not None
                        else "",
                        output_json=json.dumps(resolved_data["out"], ensure_ascii=False, sort_keys=True)
                        if resolved_data["out"] is not None
                        else "",
                    )
                )
        return resolved_events

    def list_evaluations(self, trace_id: str = "", *, limit: int = 50) -> list[TraceEvaluation]:
        if trace_id:
            query = """
                SELECT id, trace_id, outcome, failure_domain, evidence_json, created_at
                FROM trace_evaluations WHERE trace_id = ? ORDER BY created_at DESC LIMIT ?
                """
            params: tuple[Any, ...] = (trace_id, limit)
        else:
            query = """
                SELECT id, trace_id, outcome, failure_domain, evidence_json, created_at
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
            evidence=evidence,
        )

    def record_evaluation(
        self,
        *,
        trace_id: str,
        outcome: str,
        failure_domain: str,
        evidence: dict[str, Any] | None = None,
    ) -> TraceEvaluation:
        evaluation = TraceEvaluation(
            id=uuid.uuid4().hex,
            trace_id=trace_id,
            outcome=outcome,
            failure_domain=failure_domain,
            evidence_json=json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
            created_at=time.time(),
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO trace_evaluations(
                    id, trace_id, outcome, failure_domain, evidence_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.id,
                    evaluation.trace_id,
                    evaluation.outcome,
                    evaluation.failure_domain,
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
    draft = _evaluate_trace_with_rules(events, _base_trace_evidence(events))
    root = TraceRunView(
        id=trace_id,
        trace_id=trace_id,
        parent_run_id="",
        name="trace",
        run_type=TraceRunType.CHAIN,
        status=TraceRunStatus.SUCCESS
        if draft.outcome == str(TraceOutcome.SUCCESS)
        else TraceRunStatus.ERROR,
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
        status=_event_trace_run_status(event),
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


def _event_trace_run_status(event: TraceEvent) -> TraceRunStatus | str:
    if event.ok:
        return TraceRunStatus.SUCCESS
    facts = _event_output(event).get("facts")
    if event.phase == TracePhase.CAPABILITY_RESULT and _capability_result_is_approval_request(facts):
        return "blocked"
    return TraceRunStatus.ERROR


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
    return _evaluation(
        TraceOutcome.SUCCESS,
        TraceFailureDomain.NONE,
        evidence,
        rule="no_failed_or_degraded_rule",
    )


def _first_failure(events: list[TraceEvent]) -> TraceEvent | None:
    return next((event for event in events if not event.ok), None)


def _record_first_failure_evidence(event: TraceEvent, evidence: dict[str, Any]) -> None:
    evidence["first_failure_phase"] = event.phase
    evidence["first_failure_tool"] = event.tool


def _evaluation(
    outcome: str,
    failure_domain: str,
    evidence: dict[str, Any],
    *,
    rule: str,
) -> TraceEvaluationDraft:
    evidence["evaluation_rule"] = rule
    return TraceEvaluationDraft(outcome, failure_domain)


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
    decision_items = tuple(
        (
            loop_decision_summary(
                output,
                event_tool=event.tool,
                event_run_id=event.run_id,
            ),
            output,
        )
        for event, output in decisions
    )

    for classifier in LOOP_DECISION_EVALUATION_RULES:
        for summary, output in reversed(decision_items):
            draft = classifier(summary, output, events, evidence)
            if draft is not None:
                return draft

    return None


def _failed_loop_decision_rule(
    summary: LoopDecisionSummary,
    output: dict[str, Any],
    events: list[TraceEvent],
    evidence: dict[str, Any],
) -> TraceEvaluationDraft | None:
    if summary.decision != LoopDecisionKind.FAILED:
        return None
    failure_domain = classify_loop_failure(output)
    rule = "loop_decision_failed"
    if failure_domain == TraceFailureDomain.CAPABILITY_FAILURE:
        first_failure = _first_failure(events)
        if first_failure is not None and _capability_result_is_input_schema_mismatch(first_failure):
            evidence["failure_domain_corrected_from"] = str(
                TraceFailureDomain.CAPABILITY_FAILURE
            )
            failure_domain = TraceFailureDomain.PLANNER_OR_PARSER
            rule = "loop_failure_domain_corrected_by_input_schema"
    return _evaluation(
        TraceOutcome.FAILURE,
        failure_domain,
        evidence,
        rule=rule,
    )


def _blocked_or_approval_loop_decision_rule(
    summary: LoopDecisionSummary,
    output: dict[str, Any],
    events: list[TraceEvent],
    evidence: dict[str, Any],
) -> TraceEvaluationDraft | None:
    del events
    if summary.decision == LoopDecisionKind.BLOCKED:
        return _evaluation(
            TraceOutcome.FAILURE,
            classify_loop_blocked(output),
            evidence,
            rule="loop_decision_blocked",
        )
    if is_approval_loop_decision(output):
        return _evaluation(
            TraceOutcome.DEGRADED,
            TraceFailureDomain.APPROVAL_LOOP,
            evidence,
            rule="approval_loop_decision",
        )
    return None


def _converged_loop_decision_rule(
    summary: LoopDecisionSummary,
    output: dict[str, Any],
    events: list[TraceEvent],
    evidence: dict[str, Any],
) -> TraceEvaluationDraft | None:
    del output, events
    if summary.decision != LoopDecisionKind.CONVERGED:
        return None
    return _evaluation(
        TraceOutcome.DEGRADED,
        TraceFailureDomain.LOOP_NO_PROGRESS,
        evidence,
        rule="loop_decision_converged",
    )


LOOP_DECISION_EVALUATION_RULES: tuple[LoopDecisionEvaluationRule, ...] = (
    _failed_loop_decision_rule,
    _blocked_or_approval_loop_decision_rule,
    _converged_loop_decision_rule,
)


def _first_failure_rule(
    events: list[TraceEvent],
    evidence: dict[str, Any],
    *,
    phase: str,
    failure_domain: str,
    rule: str,
    predicate: Callable[[TraceEvent], bool] | None = None,
) -> TraceEvaluationDraft | None:
    failure = _first_failure(events)
    if failure is None or failure.phase != phase:
        return None
    if predicate is not None and not predicate(failure):
        return None
    _record_first_failure_evidence(failure, evidence)
    return _evaluation(TraceOutcome.FAILURE, failure_domain, evidence, rule=rule)


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
    return _evaluation(
        TraceOutcome.FAILURE,
        TraceFailureDomain.PLANNER_OR_PARSER,
        evidence,
        rule="planner_failed_event",
    )


def _safeguard_failure_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    return _first_failure_rule(
        events,
        evidence,
        phase=TracePhase.CAPABILITY_RESULT,
        failure_domain=TraceFailureDomain.SAFEGUARD_POLICY,
        rule="safeguard_hook_decision",
        predicate=_capability_result_has_safeguard_decision,
    )


def _capability_failure_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    failure = _first_failure(events)
    if failure is None or failure.phase != TracePhase.CAPABILITY_RESULT:
        return None
    _record_first_failure_evidence(failure, evidence)
    if _capability_result_is_input_schema_mismatch(failure):
        return _evaluation(
            TraceOutcome.FAILURE,
            TraceFailureDomain.PLANNER_OR_PARSER,
            evidence,
            rule="capability_input_schema_mismatch",
        )
    return _evaluation(
        TraceOutcome.FAILURE,
        TraceFailureDomain.CAPABILITY_FAILURE,
        evidence,
        rule="capability_failed_event",
    )


def _approval_pause_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    if not _has_approval_required_pause(events):
        return None
    failure = _first_failure(events)
    if failure is not None:
        if failure.phase != TracePhase.CAPABILITY_RESULT:
            return None
        facts = _event_output(failure).get("facts")
        if not _capability_result_is_approval_request(facts):
            return None
        _record_first_failure_evidence(failure, evidence)
    evidence["approval_pause_recorded"] = True
    return _evaluation(
        TraceOutcome.SUCCESS,
        TraceFailureDomain.NONE,
        evidence,
        rule="approval_pause_recorded",
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
        rule = "checker_failed_after_recovery_plan"
    else:
        evidence["recovery_plan_recorded"] = False
        rule = "checker_failed_without_recovery_plan"
    return _evaluation(TraceOutcome.FAILURE, TraceFailureDomain.CHECKER_BLOCKED, evidence, rule=rule)


def _runtime_failure_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    failure = _first_failure(events)
    if failure is None:
        return None
    _record_first_failure_evidence(failure, evidence)
    return _evaluation(
        TraceOutcome.FAILURE,
        TraceFailureDomain.RUNTIME,
        evidence,
        rule="runtime_failed_event",
    )


def _planner_no_response_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    if not _planner_call_started_without_result(events):
        return None
    evidence["planner_call_without_result"] = True
    return _evaluation(
        TraceOutcome.FAILURE,
        TraceFailureDomain.PROVIDER_NO_RESPONSE,
        evidence,
        rule="planner_call_without_result",
    )


def _pending_completion_gap_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    if not _has_unverified_pending_run_completion(events):
        return None
    evidence["pending_run_completion_risk"] = True
    return _evaluation(
        TraceOutcome.DEGRADED,
        TraceFailureDomain.MISSING_COMPLETION_CHECK,
        evidence,
        rule="pending_run_completion_gap",
    )


def _missing_trace_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    if events:
        return None
    return _evaluation(
        TraceOutcome.UNKNOWN,
        TraceFailureDomain.TRACE_MISSING,
        evidence,
        rule="trace_missing",
    )


TRACE_EVALUATION_RULES: tuple[TraceEvaluationRule, ...] = (
    _loop_decision_rule,
    _planner_failure_rule,
    _safeguard_failure_rule,
    _approval_pause_rule,
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


def _capability_result_is_approval_request(facts: Any) -> bool:
    return (
        isinstance(facts, dict)
        and str(facts.get("entity_type") or "") == "approval_request"
        and isinstance(facts.get("approval"), dict)
    )


def _capability_result_is_input_schema_mismatch(event: TraceEvent) -> bool:
    output = _event_output(event)
    facts = output.get("facts")
    if not isinstance(facts, dict):
        return False
    if facts.get(CAPABILITY_ERROR_REASON_KEY) != "schema_mismatch":
        return False
    return "result_action" not in facts


def _has_approval_required_pause(events: list[TraceEvent]) -> bool:
    for event, output in reversed(_loop_decision_events(events)):
        summary = loop_decision_summary(
            output,
            event_tool=event.tool,
            event_run_id=event.run_id,
        )
        if summary.decision != str(LoopDecisionKind.PAUSE_FOR_APPROVAL):
            continue
        if summary.reason != str(LoopReason.APPROVAL_REQUIRED):
            continue
        if summary.failure_domain not in {"", str(TraceFailureDomain.NONE)}:
            continue
        if _loop_results_include(
            output.get("gate_results"),
            name=str(LoopCheckName.APPROVAL_GATE),
            passed=True,
        ):
            return True
    return False


def _loop_results_include(value: Any, *, name: str, passed: bool) -> bool:
    if not isinstance(value, list):
        return False
    return any(
        isinstance(item, dict)
        and str(item.get("name") or "") == name
        and item.get("passed") is passed
        for item in value
    )


def _planner_call_started_without_result(events: list[TraceEvent]) -> bool:
    phases = [event.phase for event in events]
    return TracePhase.PLANNER_CALL_START in phases and not any(
        phase in {TracePhase.PLANNER_SYSCALL, TracePhase.PLANNER_CALL_ERROR, TracePhase.TURN_FINAL}
        for phase in phases
    )

def _extract_blobs(data: Any, insert_blob: Callable[[str, str], None], max_len: int = 1024) -> Any:
    if isinstance(data, dict):
        return {k: _extract_blobs(v, insert_blob, max_len) for k, v in data.items()}
    if isinstance(data, list):
        return [_extract_blobs(v, insert_blob, max_len) for v in data]
    if isinstance(data, str) and len(data) > max_len:
        digest = hashlib.md5(data.encode("utf-8")).hexdigest()
        insert_blob(digest, data)
        return {"$blob": digest}
    return data

def _resolve_blobs(data: Any, fetch_blobs: Callable[[set[str]], dict[str, str]]) -> Any:
    hashes = set()
    def _find_hashes(d: Any) -> None:
        if isinstance(d, dict):
            if len(d) == 1 and "$blob" in d:
                hashes.add(d["$blob"])
            else:
                for v in d.values():
                    _find_hashes(v)
        elif isinstance(d, list):
            for v in d:
                _find_hashes(v)
    _find_hashes(data)
    if not hashes:
        return data

    blob_map = fetch_blobs(hashes)

    def _replace(d: Any) -> Any:
        if isinstance(d, dict):
            if len(d) == 1 and "$blob" in d:
                h = d["$blob"]
                return blob_map.get(h, f"<missing blob: {h}>")
            return {k: _replace(v) for k, v in d.items()}
        if isinstance(d, list):
            return [_replace(v) for v in d]
        return d
    return _replace(data)


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
        Column("evidence_json", "TEXT", nullable=False),
        Column("created_at", "REAL", nullable=False),
    ],
)
TRACE_BLOBS_TABLE = Table(
    "trace_blobs",
    [
        Column("hash", "TEXT", primary_key=True),
        Column("content", "TEXT", nullable=False),
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
    return (
        completion_block_reason(
            home=None,
            events=_trace_fact_events(events),
            checks=(delegation_event_incomplete,),
        )
        is not None
    )


def _trace_fact_events(events: list[TraceEvent]) -> list[dict[str, Any]]:
    fact_events: list[dict[str, Any]] = []
    for event in events:
        if event.phase != TracePhase.CAPABILITY_RESULT or not event.ok:
            continue
        facts = _event_output(event).get("facts")
        if not isinstance(facts, dict):
            continue
        event_facts = dict(facts)
        if event.run_id and not event_facts.get("run_id"):
            event_facts["run_id"] = event.run_id
        fact_events.append({"facts": event_facts})
    return fact_events
