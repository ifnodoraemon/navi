from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import hashlib
from .capability_contract import CAPABILITY_ERROR_REASON_KEY
from .db import connect, check_schema_version, write_schema_version
from .json_utils import json_object
from .loop import (
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
    loop_decision_ok,
    loop_decision_summary,
)
from .paths import db_paths
from .schema import Column, Table


TRACE_STORE_SCHEMA_VERSION = 3
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


def _trace_event_from_row(row: Any) -> TraceEvent:
    return TraceEvent(
        id=row[0],
        trace_id=row[1],
        session_id=row[2],
        run_id=row[3],
        phase=row[4],
        source=row[5],
        peer_id=row[6],
        sender_id=row[7],
        tool=row[8],
        model_role=row[9],
        ok=bool(row[10]),
        input_json=row[11],
        output_json=row[12],
        message=row[13],
        created_at=row[14],
    )


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
        maintenance_needed = False
        with connect(self.db_path) as conn:
            had_trace_events = _table_exists(conn, "trace_events")
            previous_version = _schema_version(conn, "traces")
            maintenance_needed = had_trace_events and (
                previous_version is None or previous_version < TRACE_STORE_SCHEMA_VERSION
            )
            check_schema_version(conn, "traces", TRACE_STORE_SCHEMA_VERSION)
            conn.execute(TRACE_EVENTS_TABLE.ddl)
            _ensure_schema_current(conn, TRACE_EVENTS_TABLE)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_trace_events_trace ON trace_events(trace_id, created_at)"
            )
            conn.execute(TRACE_EVALUATIONS_TABLE.ddl)
            _ensure_schema_current(conn, TRACE_EVALUATIONS_TABLE)
            conn.execute(
                """
                DELETE FROM trace_evaluations
                WHERE rowid NOT IN (
                    SELECT rowid FROM (
                        SELECT rowid,
                               ROW_NUMBER() OVER (
                                   PARTITION BY trace_id
                                   ORDER BY created_at DESC, rowid DESC
                               ) AS position
                        FROM trace_evaluations
                    ) WHERE position = 1
                )
                """
            )
            # Version 2 used this name for a non-unique lookup index. Replace
            # it explicitly so existing databases gain the UPSERT constraint.
            conn.execute("DROP INDEX IF EXISTS idx_trace_evaluations_trace")
            conn.execute(
                "CREATE UNIQUE INDEX idx_trace_evaluations_trace "
                "ON trace_evaluations(trace_id)"
            )
            conn.execute(TRACE_BLOBS_TABLE.ddl)
            _ensure_schema_current(conn, TRACE_BLOBS_TABLE)
            write_schema_version(conn, "traces", TRACE_STORE_SCHEMA_VERSION)
        if maintenance_needed:
            self._run_startup_maintenance()

    def _run_startup_maintenance(self) -> None:
        try:
            self._redact_existing_trace_data()
        except sqlite3.OperationalError as exc:
            if _sqlite_locked(exc):
                return
            raise

    def clean_old_traces(self, days: int = 30) -> None:
        """Deletes traces older than the specified number of days to prevent DB bloat."""
        cutoff = __import__("time").time() - (days * 24 * 3600)
        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM trace_events WHERE created_at < ?", (cutoff,))
            conn.execute("DELETE FROM trace_evaluations WHERE created_at < ?", (cutoff,))
            self._gc_blobs(conn)

    def _gc_blobs(self, conn: Any) -> None:
        all_blobs = {row[0] for row in conn.execute("SELECT hash FROM trace_blobs").fetchall()}
        if not all_blobs:
            return
            
        import re
        blob_pattern = re.compile(r'"\$blob"\s*:\s*"([^"]+)"')
        used_blobs = set()
        
        for row in conn.execute("SELECT input_json, output_json FROM trace_events").fetchall():
            if row[0]:
                used_blobs.update(blob_pattern.findall(row[0]))
            if row[1]:
                used_blobs.update(blob_pattern.findall(row[1]))
                
        orphaned = list(all_blobs - used_blobs)
        if orphaned:
            for i in range(0, len(orphaned), 900):
                chunk = orphaned[i : i + 900]
                placeholders = ",".join("?" * len(chunk))
                conn.execute(f"DELETE FROM trace_blobs WHERE hash IN ({placeholders})", tuple(chunk))

    def _redact_existing_trace_data(self) -> None:
        with connect(self.db_path) as conn:
            for event_id, input_json, output_json, message in conn.execute(
                "SELECT id, input_json, output_json, message FROM trace_events"
            ).fetchall():
                redacted_input = _redact_json_text(input_json)
                redacted_output = _redact_json_text(output_json)
                redacted_message = str(_redact(message))
                if (
                    redacted_input != input_json
                    or redacted_output != output_json
                    or redacted_message != message
                ):
                    conn.execute(
                        """
                        UPDATE trace_events
                        SET input_json = ?, output_json = ?, message = ?
                        WHERE id = ?
                        """,
                        (redacted_input, redacted_output, redacted_message, event_id),
                    )
            for evaluation_id, evidence_json in conn.execute(
                "SELECT id, evidence_json FROM trace_evaluations"
            ).fetchall():
                redacted_evidence = _redact_json_text(evidence_json)
                if redacted_evidence != evidence_json:
                    conn.execute(
                        "UPDATE trace_evaluations SET evidence_json = ? WHERE id = ?",
                        (redacted_evidence, evaluation_id),
                    )
            for blob_hash, content in conn.execute("SELECT hash, content FROM trace_blobs").fetchall():
                redacted_content = str(_redact(content))
                if redacted_content != content:
                    conn.execute(
                        "UPDATE trace_blobs SET content = ? WHERE hash = ?",
                        (redacted_content, blob_hash),
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
        blobs_to_insert = {}
        def _insert_blob(h: str, c: str) -> None:
            blobs_to_insert[h] = c

        input_data_extracted = _extract_blobs(_redact(input_data or {}), _insert_blob)
        output_data_extracted = _extract_blobs(_redact(output_data or {}), _insert_blob)
        redacted_message = str(_redact(message))

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
            input_json=json.dumps(input_data_extracted, ensure_ascii=False, sort_keys=True, default=str),
            output_json=json.dumps(output_data_extracted, ensure_ascii=False, sort_keys=True, default=str),
            message=redacted_message,
            created_at=time.time(),
        )
        try:
            with connect(self.db_path) as conn:
                for h, c in blobs_to_insert.items():
                    conn.execute(
                        "INSERT OR IGNORE INTO trace_blobs(hash, content) VALUES (?, ?)",
                        (h, c),
                    )
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
        except sqlite3.OperationalError as exc:
            if not _sqlite_locked(exc):
                raise
        return replace(
            event,
            input_json=json.dumps(_redact(input_data or {}), ensure_ascii=False, sort_keys=True, default=str),
            output_json=json.dumps(_redact(output_data or {}), ensure_ascii=False, sort_keys=True, default=str),
            message=redacted_message,
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

    def list_events(self, trace_id: str, *, limit: int = 5000, offset: int = 0) -> list[TraceEvent]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, trace_id, session_id, run_id, phase, source, peer_id,
                       sender_id, tool, model_role, ok, input_json, output_json,
                       message, created_at
                FROM trace_events WHERE trace_id = ? ORDER BY created_at ASC LIMIT ? OFFSET ?
                """,
                (trace_id, limit, offset),
            ).fetchall()
        events = [_trace_event_from_row(row) for row in rows]
        return self._resolve_events_blobs(events)

    def list_loop_decisions(self, trace_id: str, *, limit: int = 5000, offset: int = 0) -> list[TraceEvent]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, trace_id, session_id, run_id, phase, source, peer_id,
                       sender_id, tool, model_role, ok, input_json, output_json,
                       message, created_at
                FROM trace_events
                WHERE trace_id = ? AND phase = ?
                ORDER BY created_at ASC LIMIT ? OFFSET ?
                """,
                (trace_id, LOOP_DECISION_PHASE, limit, offset),
            ).fetchall()
        events = [_trace_event_from_row(row) for row in rows]
        return self._resolve_events_blobs(events)

    def list_run_views(self, trace_id: str, *, limit: int = 5000, offset: int = 0) -> list[TraceRunView]:
        events = self.list_events(trace_id, limit=limit, offset=offset)
        views = _trace_run_views(events, trace_id=trace_id)
        return _merge_run_views(views, _loop_run_views_for_trace(self.home, trace_id, events))

    def list_loop_run_details(self, trace_id: str, *, limit: int = 5000) -> list[dict[str, Any]]:
        events = self.list_events(trace_id, limit=limit)
        return _loop_run_details_for_trace(self.home, events)

    def list_events_for_run_or_session(
        self,
        *,
        run_id: str,
        session_id: str = "",
        limit: int = 5000,
        offset: int = 0,
    ) -> list[TraceEvent]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, trace_id, session_id, run_id, phase, source, peer_id,
                       sender_id, tool, model_role, ok, input_json, output_json,
                       message, created_at
                FROM trace_events
                WHERE run_id = ? OR (? != '' AND session_id = ?)
                ORDER BY created_at ASC LIMIT ? OFFSET ?
                """,
                (run_id, session_id, session_id, limit, offset),
            ).fetchall()
        events = [_trace_event_from_row(row) for row in rows]
        return self._resolve_events_blobs(events)

    def list_trace_meta(self, *, limit: int = 50, offset: int = 0, has_error: bool | None = None, query: str = "") -> list[dict[str, Any]]:
        base_query = """
            SELECT
                trace_id,
                MIN(created_at) as start_time,
                MAX(created_at) as end_time,
                COUNT(id) as step_count,
                SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) as failed_event_count
            FROM trace_events
        """
        params = []
        if query:
            base_query += " WHERE trace_id IN (SELECT DISTINCT trace_id FROM trace_events WHERE trace_id LIKE ? OR input_json LIKE ? OR message LIKE ?)"
            like_query = f"%{query}%"
            params.extend([like_query, like_query, like_query])
        
        base_query += """
            GROUP BY trace_id
            ORDER BY end_time DESC
        """
        with connect(self.db_path) as conn:
            rows = conn.execute(base_query, params).fetchall()

        metas: list[dict[str, Any]] = []
        skipped = 0
        for trace_id, start_time, end_time, step_count, failed_event_count in rows:
            outcome, failure_domain = self._trace_list_outcome(trace_id)
            trace_has_error = outcome == str(TraceOutcome.FAILURE)
            trace_has_issue = outcome != str(TraceOutcome.SUCCESS)
            if has_error is not None and trace_has_error is not has_error:
                continue
            if skipped < offset:
                skipped += 1
                continue
            metas.append(
                {
                    "trace_id": trace_id,
                    "has_error": trace_has_error,
                    "has_issue": trace_has_issue,
                    "outcome": outcome,
                    "failure_domain": failure_domain,
                    "start_time": start_time,
                    "end_time": end_time,
                    "duration": max(0.0, end_time - start_time),
                    "step_count": step_count,
                    "failed_event_count": failed_event_count,
                }
            )
            if len(metas) >= limit:
                break

        if metas:
            import json
            trace_ids = [m["trace_id"] for m in metas]
            first_events = []
            for i in range(0, len(trace_ids), 900):
                chunk = trace_ids[i : i + 900]
                placeholders = ",".join("?" * len(chunk))
                with connect(self.db_path) as conn:
                    rows = conn.execute(
                        f"""
                        SELECT trace_id, input_json, message, session_id
                        FROM trace_events
                        WHERE trace_id IN ({placeholders}) AND phase IN ('channel.ingress', 'turn.start')
                        ORDER BY created_at ASC
                        """,
                        chunk,
                    ).fetchall()
                    first_events.extend(rows)

            first_by_trace = {}
            thread_by_trace = {}
            for row in first_events:
                tid = row[0]
                if tid not in first_by_trace:
                    first_by_trace[tid] = (row[1], row[2])
                if row[3] and tid not in thread_by_trace:
                    thread_by_trace[tid] = row[3]

            input_hashes = {v[0] for v in first_by_trace.values() if v[0] and v[0].startswith("blob:")}
            blobs = self._fetch_blobs(input_hashes) if input_hashes else {}

            first_event_map = {}
            for meta in metas:
                tid = meta["trace_id"]
                preview_text = ""
                raw_input = None
                msg = ""
                if tid in first_by_trace:
                    raw_input, msg = first_by_trace[tid]
                    if raw_input and raw_input.startswith("blob:"):
                        raw_input = blobs.get(raw_input, raw_input)
                if raw_input and raw_input.strip() and raw_input != "{}":
                    try:
                        parsed = json.loads(raw_input)
                        preview_text = parsed.get("text", parsed.get("message", msg))
                    except Exception:
                        pass
                if not preview_text:
                    preview_text = msg
                first_event_map[tid] = preview_text
            for meta in metas:
                meta["preview_text"] = first_event_map.get(meta["trace_id"], "")
                meta["thread_id"] = thread_by_trace.get(meta["trace_id"], "")

        return metas

    def delete_traces(self, trace_id: str | None = None) -> None:
        with connect(self.db_path) as conn:
            if trace_id:
                conn.execute("DELETE FROM trace_events WHERE trace_id = ?", (trace_id,))
                conn.execute("DELETE FROM trace_evaluations WHERE trace_id = ?", (trace_id,))
                self._gc_blobs(conn)
            else:
                conn.execute("DELETE FROM trace_events")
                conn.execute("DELETE FROM trace_evaluations")
                conn.execute("DELETE FROM trace_blobs")

    def list_trace_ids(self, *, limit: int = 50, offset: int = 0, has_error: bool | None = None) -> list[str]:
        return [m["trace_id"] for m in self.list_trace_meta(limit=limit, offset=offset, has_error=has_error)]

    def _trace_list_outcome(self, trace_id: str) -> tuple[str, str]:
        latest = self.list_evaluations(trace_id, limit=1)
        if latest:
            evaluation = latest[0]
            return evaluation.outcome, evaluation.failure_domain
        events = self.list_events(trace_id)
        draft = _evaluate_trace_with_rules(events, _base_trace_evidence(events))
        return draft.outcome, draft.failure_domain

    def _fetch_blobs(self, hashes: set[str]) -> dict[str, str]:
        if not hashes:
            return {}
        hashes_list = list(hashes)
        result = {}
        for i in range(0, len(hashes_list), 900):
            chunk = hashes_list[i : i + 900]
            placeholders = ",".join("?" for _ in chunk)
            with connect(self.db_path) as conn:
                rows = conn.execute(
                    f"SELECT hash, content FROM trace_blobs WHERE hash IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    result[row[0]] = row[1]
        return result

    def _resolve_events_blobs(self, events: list[TraceEvent]) -> list[TraceEvent]:
        hashes = set()
        parsed_data: list[tuple[TraceEvent, dict[str, Any | None] | None]] = []
        for e in events:
            if (e.input_json and '"$blob"' in e.input_json) or (
                e.output_json and '"$blob"' in e.output_json
            ):
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
        for e, parsed in parsed_data:
            if parsed is None:
                resolved_events.append(e)
            else:
                resolved_data = _replace(parsed)
                resolved_events.append(
                    replace(
                        e,
                        input_json=json.dumps(resolved_data.get("in"), ensure_ascii=False, sort_keys=True)
                        if resolved_data.get("in") is not None
                        else "",
                        output_json=json.dumps(resolved_data.get("out"), ensure_ascii=False, sort_keys=True)
                        if resolved_data.get("out") is not None
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
            evidence_json=json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True, default=str),
            created_at=time.time(),
        )
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO trace_evaluations(
                    id, trace_id, outcome, failure_domain, evidence_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(trace_id) DO UPDATE SET
                    outcome = excluded.outcome,
                    failure_domain = excluded.failure_domain,
                    evidence_json = excluded.evidence_json,
                    created_at = excluded.created_at
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
            row = conn.execute(
                """
                SELECT id, trace_id, outcome, failure_domain, evidence_json, created_at
                FROM trace_evaluations WHERE trace_id = ?
                """,
                (trace_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("trace evaluation was not persisted")
        return TraceEvaluation(*row)


def _redact(value: Any) -> Any:
    from .safeguards import redact_personal_data_deep

    return redact_personal_data_deep(value)


def _redact_json_text(text: str) -> str:
    try:
        parsed = json.loads(text or "{}")
    except json.JSONDecodeError:
        return str(_redact(text or ""))
    return json.dumps(_redact(parsed), ensure_ascii=False, sort_keys=True, default=str)


def _event_output(event: TraceEvent) -> dict[str, Any]:
    return json_object(event.output_json)


def _event_input(event: TraceEvent) -> dict[str, Any]:
    return json_object(event.input_json)


def _event_facts(event: TraceEvent) -> dict[str, Any]:
    facts = _event_output(event).get("facts")
    return facts if isinstance(facts, dict) else {}


def _trace_run_views(events: list[TraceEvent], *, trace_id: str) -> list[TraceRunView]:
    if not events:
        return []

    total_prompt_tokens = 0
    total_completion_tokens = 0

    for event in events:
        out = _event_output(event)
        if out and "usage" in out and isinstance(out["usage"], dict):
            u = out["usage"]
            total_prompt_tokens += u.get("prompt_tokens", 0)
            total_completion_tokens += u.get("completion_tokens", 0)

    first_session_id = next((event.session_id for event in events if event.session_id), "")
    draft = _evaluate_trace_with_rules(events, _base_trace_evidence(events))
    root = TraceRunView(
        id=trace_id,
        trace_id=trace_id,
        parent_run_id="",
        name="Trace",
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
            "usage": {
                "prompt_tokens": total_prompt_tokens,
                "completion_tokens": total_completion_tokens,
                "total_tokens": total_prompt_tokens + total_completion_tokens,
            }
        },
    )

    views: list[TraceRunView] = [root]
    current_turn_id: str = trace_id
    current_step_id: str | None = None
    pending_llm_run: TraceRunView | None = None
    step_count = 0

    for event in events:
        if event.phase == str(TracePhase.CHANNEL_INGRESS):
            ev_view = replace(
                _event_run_view(event, parent_run_id=trace_id),
                name="Channel Receive",
                run_type=TraceRunType.CHAIN,
            )
            views.append(ev_view)
            continue

        if event.phase == str(TracePhase.CHANNEL_EGRESS):
            ev_view = replace(
                _event_run_view(event, parent_run_id=trace_id),
                name="Channel Send",
                run_type=TraceRunType.CHAIN,
            )
            views.append(ev_view)
            continue

        if event.phase == str(TracePhase.TURN_START):
            current_turn_id = f"turn_{event.id}"
            turn_view = replace(
                _event_run_view(event, parent_run_id=trace_id),
                id=current_turn_id,
                name="Turn",
                run_type=TraceRunType.CHAIN,
            )
            views.append(turn_view)
            current_step_id = None
            step_count = 0
            continue

        if event.phase == str(TracePhase.PLANNER_CALL_START):
            step_count += 1
            current_step_id = f"step_{event.id}"
            step_view = TraceRunView(
                id=current_step_id,
                trace_id=trace_id,
                parent_run_id=current_turn_id,
                name=f"Step {step_count}",
                run_type=TraceRunType.CHAIN,
                status=TraceRunStatus.SUCCESS,
                start_time=event.created_at,
                end_time=event.created_at,
            )
            views.append(step_view)

            pending_llm_run = replace(
                _event_run_view(event, parent_run_id=current_step_id),
                id=f"llm_{event.id}",
                name="Planner Reasoning",
                run_type=TraceRunType.LLM,
            )
            continue

        if event.phase in (str(TracePhase.PLANNER_SYSCALL), str(TracePhase.PLANNER_CALL_ERROR), str(TracePhase.PLANNER_PARSE_ERROR)):
            if pending_llm_run:
                new_inputs = {**(pending_llm_run.inputs or {}), **(_event_input(event) or {})}
                pending_llm_run = replace(
                    pending_llm_run,
                    end_time=event.created_at,
                    inputs=new_inputs,
                    outputs=_event_output(event),
                    status=_event_trace_run_status(event),
                )
                views.append(pending_llm_run)
                pending_llm_run = None
            else:
                parent = current_step_id or current_turn_id
                views.append(_event_run_view(event, parent_run_id=parent))
            continue

        parent = current_step_id or current_turn_id
        ev_view = _event_run_view(event, parent_run_id=parent)

        if event.phase == LOOP_DECISION_PHASE:
            decision_val = ev_view.outputs.get("decision", "unknown")
            ev_view = replace(
                ev_view,
                name=f"Decision: {decision_val}",
                run_type=TraceRunType.CHAIN,
            )
        elif event.phase == str(TracePhase.CAPABILITY_RESULT):
            ev_view = replace(
                ev_view,
                name=f"Tool: {event.tool}" if event.tool else "Tool Execution",
                run_type=TraceRunType.TOOL,
            )

        views.append(ev_view)

    # Patch end times and status for grouping spans
    # We iterate multiple times or do it from bottom-up
    for _ in range(2):
        for index, v in enumerate(views):
            if v.run_type == TraceRunType.CHAIN and v.id != trace_id:
                children = [c for c in views if c.parent_run_id == v.id]
                if children:
                    status = v.status
                    if any(c.status == TraceRunStatus.ERROR for c in children):
                        status = TraceRunStatus.ERROR
                    elif any(c.status == "blocked" for c in children):
                        status = "blocked"
                    views[index] = replace(
                        v,
                        start_time=min(c.start_time for c in children),
                        end_time=max(c.end_time for c in children),
                        status=status,
                    )

    return views

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


def _merge_run_views(
    base: list[TraceRunView],
    extra: list[TraceRunView],
) -> list[TraceRunView]:
    merged: dict[str, TraceRunView] = {item.id: item for item in base}
    for item in extra:
        merged.setdefault(item.id, item)
    return sorted(
        merged.values(),
        key=lambda item: (0 if not item.parent_run_id else 1, item.start_time, item.id),
    )


def _loop_run_details_for_trace(home: Path, events: list[TraceEvent]) -> list[dict[str, Any]]:
    loop_run_ids = _loop_run_ids_from_trace_events(events)
    if not loop_run_ids:
        return []
    from .loop_runs import LoopRunStore

    store = LoopRunStore(home)
    details: list[dict[str, Any]] = []
    for loop_run_id in sorted(loop_run_ids):
        state = store.get_run(loop_run_id)
        if state is None:
            continue
        checkpoints = store.list_checkpoints(loop_run_id, limit=1000)
        loop_events = store.list_events(loop_run_id, limit=1000)
        details.append(
            {
                "run_state": state.to_dict(),
                "events": [
                    {
                        "id": item.id,
                        "run_id": item.run_id,
                        "event_type": item.event_type,
                        "node": item.node,
                        "terminal_state": item.terminal_state,
                        "checkpoint_id": item.checkpoint_id,
                        "evidence": json_object(item.evidence_json),
                        "created_at": item.created_at,
                    }
                    for item in loop_events
                ],
                "checkpoints": [
                    {
                        "id": item.id,
                        "run_id": item.run_id,
                        "node": item.node,
                        "inputs": json_object(item.inputs_json),
                        "state": json_object(item.state_json),
                        "created_at": item.created_at,
                    }
                    for item in checkpoints
                ],
            }
        )
    return details


def _loop_run_views_for_trace(
    home: Path,
    trace_id: str,
    events: list[TraceEvent],
) -> list[TraceRunView]:
    details = _loop_run_details_for_trace(home, events)
    views: list[TraceRunView] = []
    for detail in details:
        state = detail["run_state"]
        loop_run_id = str(state.get("run_id") or "")
        if not loop_run_id:
            continue
        event_items = detail["events"]
        checkpoint_items = detail["checkpoints"]
        timestamps = [
            float(item["created_at"])
            for item in [*event_items, *checkpoint_items]
            if item.get("created_at")
        ]
        updated_at = float(state.get("updated_at") or 0.0)
        start_time = min(timestamps) if timestamps else updated_at
        end_time = max(timestamps) if timestamps else updated_at
        status = _loop_run_status(str(state.get("terminal_state") or ""))
        root_id = f"looprun_{loop_run_id}"
        views.append(
            TraceRunView(
                id=root_id,
                trace_id=trace_id,
                parent_run_id=trace_id,
                name=f"LoopRun: {str(state.get('node') or 'unknown')}",
                run_type="engine",
                status=status,
                start_time=start_time,
                end_time=end_time,
                inputs={
                    "goal_id": state.get("goal_id", ""),
                    "loop_spec_id": state.get("loop_spec_id", ""),
                },
                outputs=state,
                tags=("navi", "loop_run", "state_graph"),
                metadata={
                    "run_id": loop_run_id,
                    "goal_id": state.get("goal_id", ""),
                    "loop_spec_id": state.get("loop_spec_id", ""),
                    "terminal_state": state.get("terminal_state", ""),
                    "attempt": state.get("attempt", 0),
                },
            )
        )
        for item in event_items:
            terminal_state = str(item.get("terminal_state") or "")
            event_status = _loop_run_status(terminal_state) if terminal_state else "success"
            event_type = str(item.get("event_type") or "loop.event")
            views.append(
                TraceRunView(
                    id=f"loopevent_{item['id']}",
                    trace_id=trace_id,
                    parent_run_id=root_id,
                    name=_loop_event_name(event_type, str(item.get("node") or ""), terminal_state),
                    run_type="engine",
                    status=event_status,
                    start_time=float(item["created_at"]),
                    end_time=float(item["created_at"]),
                    inputs={
                        "node": item.get("node", ""),
                        "checkpoint_id": item.get("checkpoint_id", ""),
                    },
                    outputs={
                        "event_type": event_type,
                        "terminal_state": terminal_state,
                        "evidence": item.get("evidence") or {},
                    },
                    tags=("navi", "loop_event", event_type),
                    metadata={
                        "run_id": loop_run_id,
                        "checkpoint_id": item.get("checkpoint_id", ""),
                    },
                )
            )
        for item in checkpoint_items:
            views.append(
                TraceRunView(
                    id=f"loopcheckpoint_{item['id']}",
                    trace_id=trace_id,
                    parent_run_id=root_id,
                    name=f"Checkpoint: {item.get('node') or 'unknown'}",
                    run_type="engine",
                    status="success",
                    start_time=float(item["created_at"]),
                    end_time=float(item["created_at"]),
                    inputs=item.get("inputs") or {},
                    outputs={"state": item.get("state") or {}},
                    tags=("navi", "loop_checkpoint"),
                    metadata={
                        "run_id": loop_run_id,
                        "checkpoint_id": item.get("id", ""),
                        "node": item.get("node", ""),
                    },
                )
            )
    return views


def _loop_run_ids_from_trace_events(events: list[TraceEvent]) -> set[str]:
    ids: set[str] = set()
    for event in events:
        for payload in (_event_input(event), _event_output(event)):
            _collect_loop_run_ids(payload, ids)
    return ids


def _collect_loop_run_ids(value: Any, ids: set[str]) -> None:
    if isinstance(value, dict):
        raw_loop_run_id = value.get("loop_run_id")
        if isinstance(raw_loop_run_id, str) and raw_loop_run_id.strip():
            ids.add(raw_loop_run_id.strip())
        run_state = value.get("run_state")
        if isinstance(run_state, dict) and run_state.get("loop_spec_id"):
            raw_run_id = run_state.get("run_id")
            if isinstance(raw_run_id, str) and raw_run_id.strip():
                ids.add(raw_run_id.strip())
        for item in value.values():
            _collect_loop_run_ids(item, ids)
    elif isinstance(value, list):
        for item in value:
            _collect_loop_run_ids(item, ids)


def _loop_run_status(terminal_state: str) -> str:
    if not terminal_state:
        return "running"
    if terminal_state in {"blocked", "paused", "waiting_approval", "conflicted"}:
        return "blocked"
    if terminal_state in {"failed", "timed_out"}:
        return "error"
    return "success"


def _loop_event_name(event_type: str, node: str, terminal_state: str) -> str:
    if event_type == "loop.transition":
        target = terminal_state or node
        return f"Loop Transition: {target}"
    if event_type == "loop.checkpoint":
        return f"Loop Checkpoint: {node or 'unknown'}"
    return f"Loop Event: {event_type}"


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


_RECOVERY_COMPLETION_REASONS = frozenset(
    {
        str(LoopReason.COMPLETION_EVIDENCE_TRUE),
        str(LoopReason.TERMINAL_RESULT),
    }
)


def _successful_completion_after(
    events: list[TraceEvent], failure: TraceEvent
) -> LoopDecisionSummary | None:
    failure_seen = False
    for event in events:
        if event.id == failure.id:
            failure_seen = True
            continue
        if not failure_seen or event.phase != LOOP_DECISION_PHASE or not event.ok:
            continue
        output = _event_output(event)
        if not output:
            continue
        summary = loop_decision_summary(
            output,
            event_tool=event.tool,
            event_run_id=event.run_id,
        )
        if summary.decision not in {
            str(LoopDecisionKind.FINALIZE),
            str(LoopDecisionKind.CONVERGED),
        }:
            continue
        if summary.failure_domain not in {"", str(TraceFailureDomain.NONE)}:
            continue
        if summary.failed_checkers or summary.failed_gates:
            continue
        if summary.reason in _RECOVERY_COMPLETION_REASONS:
            return summary
    return None


def _record_recovery_evidence(
    recovery: LoopDecisionSummary, evidence: dict[str, Any]
) -> None:
    evidence["recovered_after_first_failure"] = True
    evidence["recovery_decision"] = recovery.to_dict()


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


def _blocked_loop_decision_rule(
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
    return None


def _waiting_approval_loop_decision_rule(
    summary: LoopDecisionSummary,
    output: dict[str, Any],
    events: list[TraceEvent],
    evidence: dict[str, Any],
) -> TraceEvaluationDraft | None:
    del output
    if summary.reason != str(LoopReason.APPROVAL_REQUIRED):
        return None
    if not _trace_contains_approval_request(events) and _trace_contains_user_ask(events):
        evidence["ordinary_ask_recorded_as_approval_gate"] = True
        return _evaluation(
            TraceOutcome.DEGRADED,
            TraceFailureDomain.SAFEGUARD_POLICY,
            evidence,
            rule="ordinary_ask_recorded_as_approval_gate",
        )
    evidence["pending_external_gate"] = True
    evidence["completion_evidence"] = False
    return _evaluation(
        TraceOutcome.SUCCESS,
        TraceFailureDomain.NONE,
        evidence,
        rule="loop_decision_waiting_approval",
    )


def _external_pause_loop_decision_rule(
    summary: LoopDecisionSummary,
    output: dict[str, Any],
    events: list[TraceEvent],
    evidence: dict[str, Any],
) -> TraceEvaluationDraft | None:
    del output, events
    if summary.reason != str(LoopReason.EXTERNAL_PAUSE):
        return None
    evidence["pending_external_action"] = True
    evidence["completion_evidence"] = False
    return _evaluation(
        TraceOutcome.SUCCESS,
        TraceFailureDomain.NONE,
        evidence,
        rule="loop_decision_external_pause",
    )


def _converged_loop_decision_rule(
    summary: LoopDecisionSummary,
    output: dict[str, Any],
    events: list[TraceEvent],
    evidence: dict[str, Any],
) -> TraceEvaluationDraft | None:
    del output
    if summary.decision != LoopDecisionKind.CONVERGED:
        return None
    has_issue = bool(summary.failed_checkers or summary.failed_gates)
    failure_domain = summary.failure_domain
    if failure_domain not in {"", str(TraceFailureDomain.NONE)}:
        has_issue = True
    if _first_failure(events) is not None and not has_issue:
        # Let the phase-specific rules classify a recovered failure as
        # degraded instead of allowing a final converged decision to hide it.
        return None
    if not has_issue:
        return _evaluation(
            TraceOutcome.SUCCESS,
            TraceFailureDomain.NONE,
            evidence,
            rule="loop_decision_converged",
        )
    return _evaluation(
        TraceOutcome.DEGRADED,
        failure_domain or TraceFailureDomain.LOOP_NO_PROGRESS,
        evidence,
        rule="loop_decision_converged_with_issue",
    )


def _duplicate_entity_mutation_rule(
    events: list[TraceEvent], evidence: dict[str, Any]
) -> TraceEvaluationDraft | None:
    seen: dict[str, int] = {}
    for event in events:
        if event.phase != TracePhase.CAPABILITY_RESULT or not event.ok:
            continue
        facts = _event_facts(event)
        for mutation_ref in _entity_mutation_refs(facts):
            seen[mutation_ref] = seen.get(mutation_ref, 0) + 1
    duplicates = {ref: count for ref, count in seen.items() if count > 1}
    if not duplicates:
        return None
    evidence["duplicate_mutation"] = {
        "refs": duplicates,
    }
    return _evaluation(
        TraceOutcome.DEGRADED,
        TraceFailureDomain.LOOP_NO_PROGRESS,
        evidence,
        rule="duplicate_entity_mutation",
    )


def _entity_mutation_refs(facts: dict[str, Any]) -> list[str]:
    refs = _single_entity_mutation_refs(facts)
    for key, value in facts.items():
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict):
                refs.extend(_single_entity_mutation_refs(item, collection=key))
    return refs


def _single_entity_mutation_refs(
    facts: dict[str, Any],
    *,
    collection: str = "",
) -> list[str]:
    transition = str(facts.get("state_transition") or "")
    if not transition or transition == "state_read":
        return []
    entity_type = str(facts.get("entity_type") or collection or "entity")
    entity_id = _entity_id_from_facts(facts)
    if not entity_id:
        return []
    return [f"{entity_type}:{entity_id}:{transition}"]


def _entity_id_from_facts(facts: dict[str, Any]) -> str:
    for key in (
        "entity_id",
        "goal_id",
        "run_id",
        "loop_run_id",
        "approval_id",
        "session_id",
        "memory_id",
        "trace_id",
        "delivery_id",
    ):
        value = str(facts.get(key) or "")
        if value:
            return value
    return ""


LOOP_DECISION_EVALUATION_RULES: tuple[LoopDecisionEvaluationRule, ...] = (
    _failed_loop_decision_rule,
    _waiting_approval_loop_decision_rule,
    _external_pause_loop_decision_rule,
    _blocked_loop_decision_rule,
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
    recovery = _successful_completion_after(events, failure)
    if recovery is not None:
        _record_recovery_evidence(recovery, evidence)
        return _evaluation(
            TraceOutcome.DEGRADED,
            TraceFailureDomain.PLANNER_OR_PARSER,
            evidence,
            rule="planner_failed_then_recovered",
        )
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
        recovery = _successful_completion_after(events, failure)
        if recovery is not None:
            _record_recovery_evidence(recovery, evidence)
            return _evaluation(
                TraceOutcome.DEGRADED,
                TraceFailureDomain.PLANNER_OR_PARSER,
                evidence,
                rule="capability_input_schema_mismatch_then_recovered",
            )
        return _evaluation(
            TraceOutcome.FAILURE,
            TraceFailureDomain.PLANNER_OR_PARSER,
            evidence,
            rule="capability_input_schema_mismatch",
        )
    recovery = _successful_completion_after(events, failure)
    if recovery is not None:
        _record_recovery_evidence(recovery, evidence)
        return _evaluation(
            TraceOutcome.DEGRADED,
            TraceFailureDomain.CAPABILITY_FAILURE,
            evidence,
            rule="capability_failed_then_recovered",
        )
    return _evaluation(
        TraceOutcome.FAILURE,
        TraceFailureDomain.CAPABILITY_FAILURE,
        evidence,
        rule="capability_failed_event",
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
    _duplicate_entity_mutation_rule,
    _loop_decision_rule,
    _planner_failure_rule,
    _safeguard_failure_rule,
    _capability_failure_rule,
    _checker_failure_rule,
    _runtime_failure_rule,
    _planner_no_response_rule,
    _missing_trace_rule,
)


def _capability_result_has_safeguard_decision(event: TraceEvent) -> bool:
    output = _event_output(event)
    facts = output.get("facts")
    return isinstance(facts, dict) and isinstance(facts.get("hook_decision"), dict)


def _trace_contains_approval_request(events: list[TraceEvent]) -> bool:
    for event in events:
        if event.phase != TracePhase.CAPABILITY_RESULT:
            continue
        facts = _event_facts(event)
        if _capability_result_is_approval_request(facts):
            return True
        if isinstance(facts.get("pending_approval"), dict):
            return True
        if isinstance(facts.get("current_approval"), dict):
            return True
        if isinstance(facts.get("approval"), dict) and str(facts.get("entity_type") or "") in {
            "approval",
            "approval_request",
        }:
            return True
    return False


def _trace_contains_user_ask(events: list[TraceEvent]) -> bool:
    for event in events:
        if event.phase != TracePhase.CAPABILITY_RESULT or not event.ok:
            continue
        output = _event_output(event)
        if event.tool == "ask.user":
            return True
        if str(output.get("action") or "") == "ask" and event.tool in {"respond", "ask.user"}:
            return True
    return False


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


def _schema_version(conn, component: str) -> int | None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_versions (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    row = conn.execute(
        "SELECT version FROM schema_versions WHERE component = ?",
        (component,),
    ).fetchone()
    return int(row[0]) if row is not None else None


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


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


def _sqlite_locked(exc: sqlite3.OperationalError) -> bool:
    return "locked" in str(exc).lower()
