from __future__ import annotations

import time
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect
from .approval_contract import (
    APPROVAL_ACTION_SESSION_ELEVATION,
    APPROVAL_DECISION_APPROVE,
    APPROVAL_DECISIONS,
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_PENDING,
    APPROVAL_STATUS_REJECTED,
)
from .lifecycle import Acceptance, Governance, Phase, Resolution
from .runs import Run, RunStore
from .runs.models import Approval
from .workflows import Workflow, WorkflowStore




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
    pending_approvals: tuple[Approval, ...]
    active_workflows: tuple[Workflow, ...]


@dataclass(frozen=True)
class ApprovalResolution:
    ok: bool
    message: str
    facts: dict[str, Any]


class ApprovalService:
    def __init__(self, home: Path):
        self.home = home

    def resolve(
        self,
        *,
        decision: str,
        selection: str,
        context: SurfaceContext,
        code: str = "",
        run_id: str = "",
    ) -> ApprovalResolution:
        runs = RunStore(self.home)
        candidates = [
            run
            for run in runs.list_by_phases([Phase.PENDING, Phase.RUNNING, Phase.PAUSED], limit=100)
            if run_matches_context(run, context)
        ]
        normalized_decision = decision.strip().lower()
        if normalized_decision not in APPROVAL_DECISIONS:
            return _approval_not_resolved(
                decision=normalized_decision,
                reason="invalid_decision",
                code_present=bool(code),
                active_run_count=len(candidates),
                selection=selection,
            )

        approval = None
        if code:
            approval = runs.pending_approval_by_code(
                code,
                source=context.source,
                peer_id=context.peer_id,
                sender_id=context.sender_id,
            )
            reason = "approval_code_not_found" if approval is None else ""
        elif run_id:
            approval = runs.pending_approval_for_run(
                run_id,
                source=context.source,
                peer_id=context.peer_id,
                sender_id=context.sender_id,
            )
            reason = "run_has_no_approval" if approval is None else ""
        else:
            reason = "approval_identifier_missing"

        if approval is None:
            return _approval_not_resolved(
                decision=normalized_decision,
                reason=reason,
                code_present=bool(code),
                active_run_count=len(candidates),
                selection=selection,
                run_id=run_id,
            )

        with connect(runs.db_path) as conn:
            resolved = runs.resolve_approval_in_transaction(
                conn,
                approval.id,
                decision=normalized_decision,
                resolved_by=context.sender_id,
            )
            if resolved is None:
                return _approval_not_resolved(
                    decision=normalized_decision,
                    reason="approval_not_pending",
                    code_present=bool(code),
                    active_run_count=len(candidates),
                    selection=selection,
                    run_id=approval.run_id,
                    approval_id=approval.id,
                )

            if normalized_decision == APPROVAL_DECISION_APPROVE:
                if resolved.action == APPROVAL_ACTION_SESSION_ELEVATION:
                    phase = Phase.ENDED
                    governance = Governance.APPROVED
                    acceptance = Acceptance.ACCEPTED
                    resolution = Resolution.SUCCESS
                    result_summary = "session_elevation_approved"
                else:
                    phase = Phase.PENDING
                    governance = Governance.APPROVED
                    acceptance = Acceptance.NONE
                    resolution = Resolution.NONE
                    result_summary = ""
                runs.update_run_in_transaction(
                    conn,
                    resolved.run_id,
                    phase=phase,
                    governance=governance,
                    acceptance=acceptance,
                    resolution=resolution,
                    result_summary=result_summary,
                    error="",
                    trust_rule_id=f"approval:{resolved.id}",
                )
                status = APPROVAL_STATUS_APPROVED
            else:
                phase = Phase.ENDED
                governance = Governance.REJECTED
                acceptance = Acceptance.REJECTED
                resolution = Resolution.FAILED
                runs.update_run_in_transaction(
                    conn,
                    resolved.run_id,
                    phase=phase,
                    governance=governance,
                    acceptance=acceptance,
                    resolution=resolution,
                    result_summary="approval_rejected",
                    error="approval rejected by user",
                )
                status = APPROVAL_STATUS_REJECTED

        facts = {
            "decision": normalized_decision,
            "selection": selection,
            "code_present": bool(code),
            "active_run_count": len(candidates),
            "run_id": resolved.run_id,
            "approval_id": resolved.id,
            "status": status,
            "run_phase": str(phase),
            "run_governance": str(governance),
            "run_acceptance": str(acceptance),
            "run_resolution": str(resolution),
            "approval_resolution": {
                "reason": status,
                "decision": normalized_decision,
                "approval_id": resolved.id,
                "run_id": resolved.run_id,
                "action": resolved.action,
                "status": status,
                "requested_tool": resolved.requested_tool,
                "requested_permission": resolved.requested_permission,
            },
        }
        message = (
            "approval_resolved\n"
            f"decision={normalized_decision}\n"
            f"status={status}\n"
            f"run_id={resolved.run_id}\n"
            f"approval_id={resolved.id}\n"
            f"run_phase={phase}\n"
            f"run_governance={governance}\n"
            f"run_resolution={resolution}"
        )
        return ApprovalResolution(ok=True, message=message, facts=facts)


class CurrentStateBuilder:
    def __init__(self, home: Path):
        self.home = home

    def build(self, context: SurfaceContext) -> CurrentState:
        runs = RunStore(self.home)
        active_runs = tuple(
            run
            for run in runs.list_by_phases([Phase.PENDING, Phase.RUNNING, Phase.PAUSED], limit=100)
            if run_matches_context(run, context)
        )
        pending_approvals = tuple(
            approval
            for approval in runs.list_approvals(
                status=APPROVAL_STATUS_PENDING,
                limit=100,
            )
            if run_matches_context(approval, context)
        )
        workflows = WorkflowStore(self.home)
        active_workflows: list[Workflow] = []
        active_workflows.extend(
            workflow
            for workflow in workflows.list(phase="running", limit=100)
            if _workflow_matches_context(workflow, context)
        )
        return CurrentState(
            surface=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            session_id=context.session_id or "",
            workspace=context.workspace,
            active_runs=active_runs,
            pending_approvals=pending_approvals,
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
                "phase": run.phase,
                "governance": run.governance,
                "acceptance": run.acceptance,
                "resolution": run.resolution,
                "kind": run.kind,
                "source": run.source,
                "peer_id": run.peer_id,
                "sender_id": run.sender_id,
                "workspace": run.workspace,
                "result_summary": _non_authoritative_run_summary(run.result_summary),
                "error": run.error,
                "updated_at": run.updated_at,
            }
            for run in state.active_runs
        ],
        "pending_approvals": [
            {
                "id": approval.id,
                "run_id": approval.run_id,
                "action": approval.action,
                "requested_tool": approval.requested_tool,
                "requested_permission": approval.requested_permission,
                "source": approval.source,
                "peer_id": approval.peer_id,
                "sender_id": approval.sender_id,
                "status": approval.status,
                "code": approval.code,
                "expires_at": approval.expires_at,
                "created_at": approval.created_at,
                "updated_at": approval.updated_at,
                "reason": approval.reason,
            }
            for approval in state.pending_approvals
        ],
        "active_workflows": [
            {
                "id": workflow.id,
                "objective": workflow.objective,
                "phase": workflow.phase,
                "governance": workflow.governance,
                "acceptance": workflow.acceptance,
                "resolution": workflow.resolution,
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


def _non_authoritative_run_summary(summary: str) -> str:
    text = summary.strip()
    if not text:
        return ""
    lines = [
        line
        for line in text.splitlines()
        if not line.strip().lower().startswith("approval_code=")
    ]
    return "\n".join(lines).strip()


def _workflow_matches_context(workflow: Workflow, context: SurfaceContext) -> bool:
    if workflow.sender_id and context.sender_id and workflow.sender_id != context.sender_id:
        return False
    if workflow.peer_id and context.peer_id and workflow.peer_id != context.peer_id:
        return False
    if workflow.source and context.source and workflow.source != context.source:
        return False
    return True


def _approval_not_resolved(
    *,
    decision: str,
    reason: str,
    code_present: bool,
    active_run_count: int,
    selection: str,
    run_id: str = "",
    approval_id: str = "",
) -> ApprovalResolution:
    facts = {
        "decision": decision,
        "selection": selection,
        "code_present": code_present,
        "active_run_count": active_run_count,
        "run_id": run_id,
        "approval_id": approval_id,
        "reason": reason,
        "approval_resolution": {
            "reason": reason,
            "decision": decision,
            "run_id": run_id,
            "approval_id": approval_id,
        },
    }
    return ApprovalResolution(
        ok=False,
        message=(
            "approval_not_resolved\n"
            f"decision={decision}\n"
            f"reason={reason}\n"
            f"active_run_count={active_run_count}"
        ),
        facts=facts,
    )
