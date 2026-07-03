from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..db import connect, check_schema_version, write_schema_version
from ..loop import (
    LoopDecision,
    TraceOutcome,
    TraceRunView,
    loop_decision_ok,
)
from ..paths import db_paths
from .models import TraceEvent, TraceEvaluation, LOOP_DECISION_PHASE
from .queries import (
    TRACE_EVENTS_TABLE,
    TRACE_EVALUATIONS_TABLE,
    TRACE_BLOBS_TABLE,
    _TRACE_EVENT_COLUMNS,
    _ensure_schema_current,
    _extract_blobs,
    _redact,
    _redact_json_text,
    _resolve_blobs,
    _trace_run_views,
    _evaluate_trace_with_rules,
    _base_trace_evidence,
)

TRACE_STORE_SCHEMA_VERSION = 1

class TraceStore:
    _db_initialized: set[Path] = set()

    def __init__(self, home: Path):
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = db_paths(home).traces
        if self.db_path not in self._db_initialized:
            self._init_db()
            TraceStore._db_initialized.add(self.db_path)

    def _init_db(self) -> None:
        print("TraceStore._init_db called", flush=True)
        with connect(self.db_path) as conn:
            print("TraceStore connected", flush=True)
            check_schema_version(conn, "traces", TRACE_STORE_SCHEMA_VERSION)
            print("TraceStore schema checked", flush=True)
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
            write_schema_version(conn, "traces", TRACE_STORE_SCHEMA_VERSION)
        self._redact_existing_trace_data()
        self.clean_old_traces()

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
        events = [TraceEvent(*row[:10], bool(row[10]), *row[11:]) for row in rows]
        return self._resolve_events_blobs(events)

    def list_loop_decisions(self, trace_id: str, *, limit: int = 5000, offset: int = 0) -> list[TraceEvent]:
        return [
            event
            for event in self.list_events(trace_id, limit=limit, offset=offset)
            if event.phase == LOOP_DECISION_PHASE
        ]

    def list_run_views(self, trace_id: str, *, limit: int = 5000, offset: int = 0) -> list[TraceRunView]:
        return _trace_run_views(self.list_events(trace_id, limit=limit, offset=offset), trace_id=trace_id)

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
        events = [TraceEvent(*row[:10], bool(row[10]), *row[11:]) for row in rows]
        return self._resolve_events_blobs(events)

    def list_trace_meta(self, *, limit: int = 50, offset: int = 0, has_error: bool | None = None, query: str = "") -> list[dict[str, Any]]:
        print("list_trace_meta starting", flush=True)
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
                        SELECT trace_id, input_json, message
                        FROM trace_events
                        WHERE trace_id IN ({placeholders}) AND phase IN ('channel.ingress', 'turn.start')
                        ORDER BY created_at ASC
                        """,
                        chunk,
                    ).fetchall()
                    first_events.extend(rows)

            first_by_trace = {}
            for row in first_events:
                tid = row[0]
                if tid not in first_by_trace:
                    first_by_trace[tid] = (row[1], row[2])

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
