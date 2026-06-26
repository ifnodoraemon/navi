from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .goals import GoalStore
from .governance import GovernanceEngine
from .runs import Approval, Run, RunStore
from .workflows import (
    WORKFLOW_STATUS_APPROVED,
    WORKFLOW_STATUS_AWAITING_APPROVAL,
    WORKFLOW_STATUS_INTERRUPTED,
    WORKFLOW_STATUS_RUNNING,
    Workflow,
    WorkflowStore,
)


ApprovalDecision = Literal["approve", "reject"]
ApprovalSelection = Literal["explicit_code", "current_run", "latest_visible_batch", "all_visible"]

APPROVAL_BATCH_WINDOW_SECONDS = 30.0
PENDING_RUN_STATUSES = frozenset({"pending", "preparing", "prepared", "awaiting_approval"})
ACTIVE_WORKFLOW_STATUSES = frozenset(
    {
        WORKFLOW_STATUS_AWAITING_APPROVAL,
        WORKFLOW_STATUS_APPROVED,
        WORKFLOW_STATUS_RUNNING,
        WORKFLOW_STATUS_INTERRUPTED,
    }
)


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
class VisibleApproval:
    approval: Approval
    run: Run | None

    def facts(self, *, include_code: bool = False) -> dict[str, Any]:
        data = {
            "id": self.approval.id,
            "run_id": self.approval.run_id,
            "action": self.approval.action,
            "peer_id": self.approval.peer_id,
            "sender_id": self.approval.sender_id,
            "status": self.approval.status,
            "expires_at": self.approval.expires_at,
            "created_at": self.approval.created_at,
            "updated_at": self.approval.updated_at,
            "code_present": bool(self.approval.code),
            "run_status": self.run.status if self.run else "",
            "run_title": self.run.title if self.run else "",
            "run_source": self.run.source if self.run else "",
            "workspace": self.run.workspace if self.run else "",
        }
        if include_code:
            data["code"] = self.approval.code
        return data


@dataclass(frozen=True)
class CurrentState:
    surface: str
    peer_id: str
    sender_id: str
    session_id: str
    workspace: str
    visible_pending_approvals: tuple[VisibleApproval, ...]
    active_runs: tuple[Run, ...]
    active_workflows: tuple[Workflow, ...]

    @property
    def latest_visible_batch(self) -> tuple[VisibleApproval, ...]:
        if not self.visible_pending_approvals:
            return ()
        latest = max(item.approval.created_at for item in self.visible_pending_approvals)
        floor = latest - APPROVAL_BATCH_WINDOW_SECONDS
        return tuple(
            item for item in self.visible_pending_approvals if item.approval.created_at >= floor
        )


@dataclass(frozen=True)
class ApprovalResolution:
    ok: bool
    decision: ApprovalDecision
    selection: ApprovalSelection
    message: str
    run_id: str = ""
    facts: dict[str, Any] | None = None


class CurrentStateBuilder:
    def __init__(self, home: Path):
        self.home = home

    def build(self, context: SurfaceContext) -> CurrentState:
        runs = RunStore(self.home)
        runs.archive_expired_approvals()
        visible = self._visible_pending_approvals(runs, context)
        active_runs = tuple(
            run
            for run in runs.list_by_statuses(sorted(PENDING_RUN_STATUSES), limit=100)
            if _run_matches_context(run, context)
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
            visible_pending_approvals=tuple(visible),
            active_runs=active_runs,
            active_workflows=tuple(active_workflows),
        )

    def _visible_pending_approvals(
        self, runs: RunStore, context: SurfaceContext
    ) -> list[VisibleApproval]:
        now = time.time()
        visible: list[VisibleApproval] = []
        for approval in runs.list_approvals(limit=500):
            if approval.status != "pending" or approval.expires_at < now:
                continue
            if approval.sender_id and context.sender_id and approval.sender_id != context.sender_id:
                continue
            if approval.peer_id and context.peer_id and approval.peer_id != context.peer_id:
                continue
            run = runs.get(approval.run_id)
            if run is not None and not _run_matches_context(run, context):
                continue
            visible.append(VisibleApproval(approval=approval, run=run))
        return sorted(visible, key=lambda item: item.approval.created_at, reverse=True)


class ApprovalService:
    def __init__(self, home: Path):
        self.home = home

    def resolve(
        self,
        *,
        decision: ApprovalDecision,
        selection: ApprovalSelection,
        context: SurfaceContext,
        code: str = "",
        run_id: str = "",
    ) -> ApprovalResolution:
        if decision not in {"approve", "reject"}:
            return ApprovalResolution(
                ok=False,
                decision=decision,
                selection=selection,
                message="approval decision must be approve or reject.",
                facts={"reason": "invalid_decision"},
            )
        status = "approved" if decision == "approve" else "rejected"
        if selection == "explicit_code":
            if not code:
                return self._missing_identifier(decision, selection)
            return self._resolve_one(
                decision=decision,
                status=status,
                selection=selection,
                context=context,
                code=code,
                run_id="",
            )
        if selection == "current_run":
            if not run_id:
                return self._missing_identifier(decision, selection)
            return self._resolve_one(
                decision=decision,
                status=status,
                selection=selection,
                context=context,
                code="",
                run_id=run_id,
            )
        state = CurrentStateBuilder(self.home).build(context)
        approvals = (
            state.latest_visible_batch
            if selection == "latest_visible_batch"
            else state.visible_pending_approvals
        )
        if not approvals:
            return ApprovalResolution(
                ok=False,
                decision=decision,
                selection=selection,
                message="No visible pending approvals match this sender, peer, and source.",
                facts={
                    "reason": "no_visible_pending_approvals",
                    "selection": selection,
                    "pending_visible_count": len(state.visible_pending_approvals),
                },
            )
        resolved: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for item in approvals:
            one = self._resolve_one(
                decision=decision,
                status=status,
                selection=selection,
                context=context,
                code=item.approval.code,
                run_id="",
            )
            target = resolved if one.ok else failures
            target.append(one.facts or {"run_id": item.approval.run_id})
        ok = bool(resolved) and not failures
        verb = "approved" if decision == "approve" else "rejected"
        message = (
            f"approval_batch decision={decision} selection={selection} resolved_count={len(resolved)} failed_count=0"
            if ok
            else f"approval_batch decision={decision} selection={selection} resolved_count={len(resolved)} failed_count={len(failures)}"
        )
        return ApprovalResolution(
            ok=ok,
            decision=decision,
            selection=selection,
            message=message,
            run_id=str(resolved[0].get("run_id") or "") if resolved else "",
            facts={
                "entity_type": "approval_batch",
                "selection": selection,
                "decision": decision,
                "resolved_count": len(resolved),
                "failed_count": len(failures),
                "resolved": resolved,
                "failures": failures,
            },
        )

    def _reissue_if_expired(
        self,
        *,
        runs: RunStore,
        decision: ApprovalDecision,
        selection: ApprovalSelection,
        context: SurfaceContext,
        candidate: Approval | None,
        run_id: str,
    ) -> ApprovalResolution | None:
        """If the user approves against an expired code, mint a fresh approval and
        pull the run back into awaiting_approval instead of dead-ending. Returns a
        resolution carrying the new code, or None when re-issue does not apply
        (rejecting, or no expired candidate)."""
        if decision != "approve":
            return None
        approval = candidate
        if approval is None and run_id:
            approval = runs.latest_approval_for_run(run_id)
        if approval is None or approval.status != "expired":
            return None
        run = runs.get(approval.run_id)
        if not _approval_matches_context(approval, run, context):
            return None
        fresh = runs.reissue_approval(
            run_id=approval.run_id,
            peer_id=approval.peer_id or context.peer_id,
            sender_id=approval.sender_id or context.sender_id,
            action=approval.action,
        )
        if fresh is None:
            # The run was approved/advanced concurrently between reading the
            # expired candidate and re-issuing. Fall back to the normal resolve
            # path, which reports the run's actual current state.
            return None
        from .connector_registry import render_approval_reply

        message = render_approval_reply(
            context.source,
            code=fresh.code,
            run_id=approval.run_id,
            action=approval.action,
        )
        return ApprovalResolution(
            ok=False,
            decision=decision,
            selection=selection,
            message=message,
            run_id=approval.run_id,
            facts={
                "entity_type": "approval_request",
                "entity_id": fresh.id,
                "state_transition": "reissued",
                "reason": "approval_reissued",
                "run_id": approval.run_id,
                "approval": {
                    "code": fresh.code,
                    "expires_at": fresh.expires_at,
                },
                "run_status": "awaiting_approval",
            },
        )

    def _resolve_one(
        self,
        *,
        decision: ApprovalDecision,
        status: str,
        selection: ApprovalSelection,
        context: SurfaceContext,
        code: str,
        run_id: str,
    ) -> ApprovalResolution:
        runs = RunStore(self.home)
        candidate = runs.get_approval(code) if code else None
        if candidate is None and run_id:
            candidate = runs.pending_approval_for_run(run_id, sender_id=context.sender_id)
        reissued = self._reissue_if_expired(
            runs=runs,
            decision=decision,
            selection=selection,
            context=context,
            candidate=candidate,
            run_id=run_id,
        )
        if reissued is not None:
            return reissued
        # Idempotent resolution: if the approval is already in the target
        # status (e.g. the user re-submits an already-approved code), return
        # ok=True instead of ok=False. Without this, the planner sees a
        # failure and re-invokes approval.resolve with the same code.
        if candidate is not None:
            target_status = status
            if candidate.status == target_status:
                run = runs.get(candidate.run_id)
                if _approval_matches_context(candidate, run, context):
                    return ApprovalResolution(
                        ok=True,
                        decision=decision,
                        selection=selection,
                        message=(
                            "approval_request "
                            f"status={target_status} run_id={candidate.run_id} "
                            "state_transition=already_resolved"
                        ),
                        run_id=candidate.run_id,
                        facts={
                            "entity_type": "approval_request",
                            "entity_id": candidate.id,
                            "state_transition": "already_resolved",
                            "run_id": candidate.run_id,
                            "approval_status": candidate.status,
                            "selection": selection,
                            "decision": decision,
                        },
                    )
        if candidate is not None:
            run = runs.get(candidate.run_id)
            if not _approval_matches_context(candidate, run, context):
                if (
                    candidate.sender_id
                    and context.sender_id
                    and candidate.sender_id != context.sender_id
                ):
                    facts = runs.approval_resolution_diagnostic(
                        code=code, run_id=run_id, sender_id=context.sender_id
                    )
                    return ApprovalResolution(
                        ok=False,
                        decision=decision,
                        selection=selection,
                        message=approval_resolution_failure_message(facts),
                        run_id=candidate.run_id,
                        facts={"approval_resolution": facts},
                    )
                return ApprovalResolution(
                    ok=False,
                    decision=decision,
                    selection=selection,
                    message="Approval exists but is not visible in the current sender, peer, and source context.",
                    run_id=candidate.run_id,
                    facts={
                        "reason": "approval_context_mismatch",
                        "run_id": candidate.run_id,
                        "peer_id": candidate.peer_id,
                        "sender_id": candidate.sender_id,
                        "run_source": run.source if run else "",
                    },
                )
        governance = GovernanceEngine(self.home)
        approval = (
            governance.resolve_code(code=code, sender_id=context.sender_id, status=status)
            if code
            else governance.resolve_task(run_id=run_id, sender_id=context.sender_id, status=status)
        )
        if approval is None:
            facts = runs.approval_resolution_diagnostic(
                code=code, run_id=run_id, sender_id=context.sender_id
            )
            return ApprovalResolution(
                ok=False,
                decision=decision,
                selection=selection,
                message=approval_resolution_failure_message(facts),
                run_id=run_id,
                facts={"approval_resolution": facts},
            )
        if approval.status == "expired":
            return ApprovalResolution(
                ok=False,
                decision=decision,
                selection=selection,
                message="Approval code expired.",
                run_id=approval.run_id,
                facts={"approval_status": "expired", "run_id": approval.run_id},
            )
        run_status = "queued" if decision == "approve" else "rejected"
        task = runs.update_run(approval.run_id, status=run_status)
        if task:
            GoalStore(self.home).update_for_run(
                task,
                evidence={
                    "run_id": task.id,
                    "run_status": task.status,
                    "approval_status": approval.status,
                    "approval_selection": selection,
                },
            )
        resolved_run_id = task.id if task else approval.run_id
        facts = {
            "entity_type": "approval_request",
            "entity_id": approval.id,
            "state_transition": "updated",
            "turn_scope": "current",
            "selection": selection,
            "decision": decision,
            "run_id": resolved_run_id,
            "approval_status": approval.status,
            "run_status": run_status,
        }
        if decision == "approve":
            message = (
                f"approval_request status=approved run_id={resolved_run_id} "
                f"run_status={run_status}"
            )
        else:
            message = (
                f"approval_request status=rejected run_id={resolved_run_id} "
                f"run_status={run_status}"
            )
        return ApprovalResolution(
            ok=True,
            decision=decision,
            selection=selection,
            message=message,
            run_id=resolved_run_id,
            facts=facts,
        )

    @staticmethod
    def _missing_identifier(
        decision: ApprovalDecision, selection: ApprovalSelection
    ) -> ApprovalResolution:
        return ApprovalResolution(
            ok=False,
            decision=decision,
            selection=selection,
            message="approval resolution requires code, run_id, or an explicit visible-batch selection.",
            facts={"reason": "approval_identifier_missing"},
        )


def approval_resolution_failure_message(facts: dict[str, Any]) -> str:
    reason = str(facts.get("reason") or "")
    messages = {
        "approval_code_not_found": "Approval code was not found.",
        "sender_mismatch": "Approval exists but belongs to a different sender.",
        "approval_not_pending": f"Approval is not pending; current status is {facts.get('status') or 'unknown'}.",
        "approval_expired": "Approval is expired.",
        "run_not_found": "Run was not found for approval resolution.",
        "run_has_no_approval": "Run has no approval request.",
        "approval_identifier_missing": "approval.resolve requires code or run_id.",
        "approval_context_mismatch": "Approval exists but is not visible in the current sender, peer, and source context.",
    }
    return messages.get(reason, "Approval could not be resolved.")


def render_visible_approvals(state: CurrentState) -> str:
    approvals = state.visible_pending_approvals
    if not approvals:
        return "No visible pending approvals match this sender, peer, and source."
    lines = ["Visible pending approvals:"]
    for item in approvals:
        run_title = item.run.title if item.run else "(run missing)"
        minutes = max(0, round((item.approval.expires_at - time.time()) / 60))
        lines.append(
            f"- code {item.approval.code}: run {item.approval.run_id}, {run_title}, expires in ~{minutes} minutes"
        )
    return "\n".join(lines)


def current_state_facts(state: CurrentState) -> dict[str, Any]:
    latest_batch = state.latest_visible_batch
    return {
        "surface": state.surface,
        "peer_id": state.peer_id,
        "sender_id": state.sender_id,
        "session_id": state.session_id,
        "workspace": state.workspace,
        "visible_pending_approvals": [
            item.facts(include_code=True) for item in state.visible_pending_approvals
        ],
        "latest_visible_approval_batch": [item.facts(include_code=True) for item in latest_batch],
        "visible_pending_approval_count": len(state.visible_pending_approvals),
        "latest_visible_batch_count": len(latest_batch),
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


def explicit_code_was_user_provided(
    *,
    code: str,
    input_text: str,
    session_user_messages: list[str] | None = None,
) -> bool:
    if not code:
        return False
    if not input_text and session_user_messages is None:
        return True
    if code in input_text:
        return True
    return any(code in content for content in (session_user_messages or [])[-2:])


def run_matches_context(record: Any, context: Any) -> bool:
    """Whether a run/watch record belongs to the caller's surface context.

    Accepts any record exposing sender_id/peer_id (and optionally source), so it
    works for both Run and Watch (Watch has no source field). Used by approval
    visibility and by delegate.list so the two views stay consistent.
    """
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


def _run_matches_context(run: Run, context: SurfaceContext) -> bool:
    return run_matches_context(run, context)


def _approval_matches_context(approval: Approval, run: Run | None, context: SurfaceContext) -> bool:
    if approval.sender_id and context.sender_id and approval.sender_id != context.sender_id:
        return False
    if approval.peer_id and context.peer_id and approval.peer_id != context.peer_id:
        return False
    if run is not None and not _run_matches_context(run, context):
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
