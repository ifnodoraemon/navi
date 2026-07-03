"""Trace run view construction."""

from __future__ import annotations
from navi.lifecycle import Phase, Governance, Acceptance, Resolution

import hashlib
from dataclasses import replace
from typing import Any, Callable

from ..json_utils import json_object
from ..loop import (
    TraceOutcome,
    TracePhase,
    TraceRunStatus,
    TraceRunType,
    TraceRunView,
)
from .models import (
    TraceEvent,
    TraceEvaluationDraft,
    LOOP_DECISION_PHASE,
)
from .evaluation_rules import _evaluate_trace_with_rules, _base_trace_evidence


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
                pending_llm_run = replace(
                    pending_llm_run,
                    end_time=event.created_at,
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
