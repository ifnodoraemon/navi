from __future__ import annotations

import time
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .lifecycle import RUN_ACTIVE_STATUSES
from .runs import Run, RunStore
from .workflows import (
    WORKFLOW_STATUS_RUNNING,
    Workflow,
    WorkflowStore,
)

ACTIVE_WORKFLOW_STATUSES = frozenset({WORKFLOW_STATUS_RUNNING})


@dataclass(frozen=True)
class SurfaceContext:
    home: Path
    source: str
    peer_id: str
    sender_id: str
    session_id: str | None = None
    workspace: str = ""
    input_text: str = ""


@dataclass(frozen=True)
class CurrentState:
    surface: str
    peer_id: str
    sender_id: str
    session_id: str
    workspace: str
    active_runs: tuple[Run, ...]
    active_workflows: tuple[Workflow, ...]


class CurrentStateBuilder:
    def __init__(self, home: Path):
        self.home = home

    def build(self, context: SurfaceContext) -> CurrentState:
        runs = RunStore(self.home)
        active_runs = tuple(
            run
            for run in runs.list_by_statuses(sorted(RUN_ACTIVE_STATUSES), limit=100)
            if run_matches_context(run, context)
        )
        workflows = WorkflowStore(self.home)
        active_workflows = []
        for status in sorted(ACTIVE_WORKFLOW_STATUSES):
            active_workflows.extend(
                workflow
                for workflow in workflows.list(status=status, limit=100)
                if _workflow_matches_context(workflow, context)
            )
        return CurrentState(
            surface=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            session_id=context.session_id or "",
            workspace=context.workspace,
            active_runs=active_runs,
            active_workflows=tuple(active_workflows),
        )


def current_state_facts(state: CurrentState) -> dict[str, Any]:
    now = time.time()
    local_now = datetime.fromtimestamp(now).astimezone()
    return {
        "current_time": {
            "unix": now,
            "iso": local_now.isoformat(),
            "timezone": local_now.tzname() or "",
            "utc_offset": local_now.strftime("%z"),
        },
        "surface": state.surface,
        "peer_id": state.peer_id,
        "sender_id": state.sender_id,
        "session_id": state.session_id,
        "workspace": state.workspace,
        "active_runs": [
            {
                "id": run.id,
                "title": run.title,
                "status": run.status,
                "kind": run.kind,
                "source": run.source,
                "peer_id": run.peer_id,
                "sender_id": run.sender_id,
                "workspace": run.workspace,
                "updated_at": run.updated_at,
            }
            for run in state.active_runs
        ],
        "active_workflows": [
            {
                "id": workflow.id,
                "objective": workflow.objective,
                "status": workflow.status,
                "source": workflow.source,
                "peer_id": workflow.peer_id,
                "sender_id": workflow.sender_id,
                "workspace": workflow.workspace,
                "updated_at": workflow.updated_at,
            }
            for workflow in state.active_workflows
        ],
    }


def run_matches_context(record: Any, context: Any) -> bool:
    record_sender = getattr(record, "sender_id", "")
    record_peer = getattr(record, "peer_id", "")
    record_source = getattr(record, "source", "")
    if record_sender and context.sender_id and record_sender != context.sender_id:
        return False
    if record_peer and context.peer_id and record_peer != context.peer_id:
        return False
    if record_source and context.source and record_source != context.source:
        return False
    return True


def _workflow_matches_context(workflow: Workflow, context: SurfaceContext) -> bool:
    if workflow.sender_id and context.sender_id and workflow.sender_id != context.sender_id:
        return False
    if workflow.peer_id and context.peer_id and workflow.peer_id != context.peer_id:
        return False
    if workflow.source and context.source and workflow.source != context.source:
        return False
    return True
