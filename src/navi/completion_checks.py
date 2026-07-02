from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .control import CurrentStateBuilder, SurfaceContext
from .lifecycle import RUN_STATUS_RUNNING
from .recovery import CompletionBlock
from .runs import Run, RunStore
from .workflows import WORKFLOW_RUNNABLE_STATUSES


CompletionCheck = Callable[["CompletionCheckFrame"], CompletionBlock | None]
# Only `running` blocks respond — a run that is `pending` (waiting for
# user confirmation via respond) is a normal pause point, not an incomplete
# delegation. `prepared` was removed in favour of the 4-state model.
RUN_EVENT_INCOMPLETE_STATUSES = frozenset({RUN_STATUS_RUNNING})
RUN_ACTIVE_INCOMPLETE_STATUSES = frozenset({RUN_STATUS_RUNNING})


@dataclass
class CompletionCheckFrame:
    home: Any
    events: list[dict[str, Any]]
    state_context: SurfaceContext | None = None
    current_run_id: str = ""
    governed_run_id: str = ""
    governed_workflow_id: str = ""
    terminal_text: str = ""
    _state: Any | None = field(default=None, init=False, repr=False)

    @property
    def related_run_ids(self) -> set[str]:
        run_ids = {self.current_run_id} if self.current_run_id else set()
        for event in self.events:
            facts = event.get("facts")
            if not isinstance(facts, dict):
                continue
            run_id = _run_id(facts)
            if run_id:
                run_ids.add(run_id)
        return run_ids

    @property
    def state(self) -> Any | None:
        if self.state_context is None:
            return None
        if self._state is None:
            self._state = CurrentStateBuilder(self.home).build(self.state_context)
        return self._state


def completion_block_reason(
    *,
    home: Any,
    events: list[dict[str, Any]],
    state_context: SurfaceContext | None = None,
    current_run_id: str = "",
    governed_run_id: str = "",
    governed_workflow_id: str = "",
    terminal_text: str = "",
    checks: tuple[CompletionCheck, ...] = (),
) -> CompletionBlock | None:
    frame = CompletionCheckFrame(
        home=home,
        events=events or [],
        state_context=state_context,
        current_run_id=current_run_id,
        governed_run_id=governed_run_id,
        governed_workflow_id=governed_workflow_id,
        terminal_text=terminal_text,
    )
    for check in checks or COMPLETION_CHECKS:
        block = check(frame)
        if block is not None:
            return block
    return None


def delegation_event_incomplete(frame: CompletionCheckFrame) -> CompletionBlock | None:
    related_run_ids = frame.related_run_ids
    latest_status = _latest_status_by_run(frame.events, related_run_ids)
    for event in frame.events:
        facts = event.get("facts")
        if not isinstance(facts, dict):
            continue
        if str(facts.get("entity_type") or "") != "delegation_run":
            continue
        run_id = _run_id(facts)
        if run_id and run_id == frame.governed_run_id:
            continue
        if not _related(run_id, related_run_ids):
            continue
        status = latest_status.get(run_id) or str(facts.get("status") or "").strip()
        if status in RUN_EVENT_INCOMPLETE_STATUSES:
            return CompletionBlock(
                reason_code="delegation_run_incomplete",
                run_id=run_id,
                run_status=status,
            )
    return None


def bulk_delete_incomplete(frame: CompletionCheckFrame) -> CompletionBlock | None:
    facts = next(
        (
            event.get("facts")
            for event in reversed(frame.events)
            if isinstance(event.get("facts"), dict)
            and event.get("facts", {}).get("entity_type") == "bulk_delete"
            and "completion_evidence" in event.get("facts", {})
        ),
        None,
    )
    if isinstance(facts, dict) and facts.get("completion_evidence") is False:
        return CompletionBlock(
            reason_code="bulk_delete_incomplete",
            details={"remaining_count": facts.get("remaining_count")},
        )
    return None


def weixin_media_delivery_missing(frame: CompletionCheckFrame) -> CompletionBlock | None:
    if frame.state_context is None:
        return None
    if frame.state_context.source not in {"weixin", "connector.weixin"}:
        return None
    if not _claims_media_delivery(frame):
        return None
    if _has_media_delivery_evidence(frame.events, frame.terminal_text):
        return None
    return CompletionBlock(
        reason_code="weixin_media_delivery_missing",
        run_id=frame.current_run_id,
        details={
            "required_tool": "connector.weixin.stage_file",
            "required_surface_directive": "MEDIA:<path>",
        },
    )


def active_run_incomplete(frame: CompletionCheckFrame) -> CompletionBlock | None:
    state = frame.state
    if state is None:
        return None
    related_run_ids = frame.related_run_ids
    for run in state.active_runs:
        if run.id == frame.governed_run_id:
            continue
        if not _related(run.id, related_run_ids):
            continue
        if run.status in RUN_ACTIVE_INCOMPLETE_STATUSES:
            return CompletionBlock(
                reason_code="delegation_run_incomplete",
                run_id=run.id,
                run_status=run.status,
            )
    return None


def active_workflow_incomplete(frame: CompletionCheckFrame) -> CompletionBlock | None:
    state = frame.state
    if state is None:
        return None
    for workflow in state.active_workflows:
        if workflow.id == frame.governed_workflow_id:
            continue
        if workflow.status in WORKFLOW_RUNNABLE_STATUSES:
            return CompletionBlock(
                reason_code="workflow_incomplete",
                details={"workflow_id": workflow.id, "workflow_status": workflow.status},
            )
    return None


COMPLETION_CHECKS: tuple[CompletionCheck, ...] = (
    delegation_event_incomplete,
    bulk_delete_incomplete,
    weixin_media_delivery_missing,
    active_run_incomplete,
    active_workflow_incomplete,
)


def _run_id(facts: dict[str, Any]) -> str:
    return str(facts.get("run_id") or facts.get("task_id") or "").strip()


def _related(run_id: str, related_run_ids: set[str]) -> bool:
    return not related_run_ids or not run_id or run_id in related_run_ids


def _latest_status_by_run(
    events: list[dict[str, Any]],
    related_run_ids: set[str],
) -> dict[str, str]:
    latest: dict[str, str] = {}
    for event in events:
        facts = event.get("facts")
        if not isinstance(facts, dict):
            continue
        run_id = _run_id(facts)
        if not _related(run_id, related_run_ids):
            continue
        status = str(
            facts.get("status") or facts.get("run_status") or facts.get("task_status") or ""
        ).strip()
        if run_id and status:
            latest[run_id] = status
    return latest


def _claims_media_delivery(frame: CompletionCheckFrame) -> bool:
    text_parts = [frame.terminal_text]
    if frame.current_run_id:
        run = RunStore(frame.home).get(frame.current_run_id)
        if run is not None:
            text_parts.extend([run.title, run.prompt])
    text = " ".join(part for part in text_parts if part).lower()
    if not text:
        return False
    delivery_terms = ("发送", "发给", "传给", "send", "sent", "deliver", "delivery")
    object_terms = (
        "简历",
        "文件",
        "图片",
        "照片",
        "视频",
        "resume",
        "file",
        "image",
        "photo",
        "video",
        ".pdf",
        ".doc",
        ".docx",
    )
    return any(term in text for term in delivery_terms) and any(
        term in text for term in object_terms
    )


def _has_media_delivery_evidence(events: list[dict[str, Any]], terminal_text: str) -> bool:
    directive = ""
    for event in events:
        if event.get("tool") != "connector.weixin.stage_file" or not event.get("ok"):
            continue
        facts = event.get("facts")
        if not isinstance(facts, dict):
            continue
        directive = str(facts.get("media_directive") or "").strip()
        if directive:
            break
    if not directive:
        return False
    return directive in terminal_text
