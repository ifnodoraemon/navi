from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .control import CurrentStateBuilder, SurfaceContext
from .lifecycle import RUN_STATUS_RUNNING
from .recovery import CompletionBlock
from .workflows import WORKFLOW_RUNNABLE_STATUSES


CompletionCheck = Callable[["CompletionCheckFrame"], CompletionBlock | None]
# Only `running` blocks final.answer — a run that is `pending` (waiting for
# user confirmation via ask.user) is a normal pause point, not an incomplete
# delegation. `prepared` was removed in favour of the 4-state model.
RUN_EVENT_INCOMPLETE_STATUSES = frozenset({RUN_STATUS_RUNNING})
RUN_ACTIVE_INCOMPLETE_STATUSES = frozenset({RUN_STATUS_RUNNING})


@dataclass
class CompletionCheckFrame:
    home: Any
    events: list[dict[str, Any]]
    state_context: SurfaceContext | None = None
    current_run_id: str = ""
    governed_workflow_id: str = ""
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
    governed_workflow_id: str = "",
    checks: tuple[CompletionCheck, ...] = (),
) -> CompletionBlock | None:
    frame = CompletionCheckFrame(
        home=home,
        events=events or [],
        state_context=state_context,
        current_run_id=current_run_id,
        governed_workflow_id=governed_workflow_id,
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


def active_run_incomplete(frame: CompletionCheckFrame) -> CompletionBlock | None:
    state = frame.state
    if state is None:
        return None
    related_run_ids = frame.related_run_ids
    for run in state.active_runs:
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
