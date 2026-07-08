from __future__ import annotations

import time
from datetime import datetime
from dataclasses import dataclass, field
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
from .loop_contracts import BudgetState, WorkspaceLock, WorkspaceState
from .loop_runs import LoopRunState
from .runs import Run, RunStore
from .runs.models import Approval
from .workspaces import ShadowWorkspaceManager, WorkspaceLockStore


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
    active_goals: tuple[Any, ...] = ()
    active_loop_runs: tuple[LoopRunState, ...] = ()
    budget_state: BudgetState = field(default_factory=BudgetState)
    workspace_state: WorkspaceState | None = None
    lock_state: tuple[WorkspaceLock, ...] = ()
    provider_state: dict[str, Any] = field(default_factory=dict)
    delegation_state: dict[str, Any] = field(default_factory=dict)
    vault_handle_state: tuple[Any, ...] = ()
    connector_state: dict[str, Any] = field(default_factory=dict)


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
        active_goals = _active_goals(self.home, context)
        active_loop_runs = _active_loop_runs(self.home, active_goals)
        active_locks = WorkspaceLockStore(self.home).list_active()
        active_shadows = _active_shadow_workspaces(self.home, context)
        vault_handles = _vault_handles(self.home)
        return CurrentState(
            surface=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            session_id=context.session_id or "",
            workspace=context.workspace,
            active_runs=active_runs,
            pending_approvals=pending_approvals,
            active_goals=active_goals,
            active_loop_runs=active_loop_runs,
            workspace_state=_workspace_state(context, active_shadows),
            lock_state=active_locks,
            vault_handle_state=vault_handles,
            connector_state={
                "source": context.source,
                "peer_id": context.peer_id,
                "sender_id": context.sender_id,
                "session_id": context.session_id or "",
            },
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
        "active_goals": [_goal_facts(goal) for goal in state.active_goals],
        "active_loop_runs": [loop_run.to_dict() for loop_run in state.active_loop_runs],
        "goal_state": {
            "active_goals": [_goal_facts(goal) for goal in state.active_goals],
        },
        "loop_run_state": {
            "active_loop_runs": [loop_run.to_dict() for loop_run in state.active_loop_runs],
        },
        "approval_state": {
            "pending_approvals": [
                {
                    "id": approval.id,
                    "run_id": approval.run_id,
                    "action": approval.action,
                    "requested_tool": approval.requested_tool,
                    "requested_permission": approval.requested_permission,
                    "status": approval.status,
                    "expires_at": approval.expires_at,
                }
                for approval in state.pending_approvals
            ],
        },
        "budget_state": state.budget_state.to_dict(),
        "workspace_state": state.workspace_state.to_dict() if state.workspace_state else {},
        "lock_state": [lock.to_dict() for lock in state.lock_state],
        "provider_state": dict(state.provider_state),
        "delegation_state": dict(state.delegation_state),
        "vault_handle_state": [handle.to_prompt_dict() for handle in state.vault_handle_state],
        "connector_state": dict(state.connector_state),
    }


def _active_goals(home: Path, context: SurfaceContext) -> tuple[Any, ...]:
    from .goals import GoalStore

    return tuple(
        goal
        for goal in GoalStore(home).list(limit=100)
        if goal.phase in {Phase.PENDING, Phase.RUNNING, Phase.PAUSED}
        and run_matches_context(goal, context)
        and _workspace_matches(goal.workspace, context.workspace)
    )


def _active_loop_runs(home: Path, active_goals: tuple[Any, ...]) -> tuple[LoopRunState, ...]:
    if not active_goals:
        return ()
    from .loop_runs import LoopRunStore

    goal_ids = {str(goal.id) for goal in active_goals}
    return tuple(
        loop_run
        for loop_run in LoopRunStore(home).list_active(limit=100)
        if loop_run.goal_id in goal_ids
    )


def _active_shadow_workspaces(home: Path, context: SurfaceContext) -> tuple[Any, ...]:
    try:
        shadows = ShadowWorkspaceManager(home).list_shadows(status="active", limit=100)
    except Exception:
        return ()
    if not context.workspace:
        return shadows
    return tuple(
        shadow
        for shadow in shadows
        if _workspace_matches(shadow.real_workspace, context.workspace)
    )


def _workspace_state(context: SurfaceContext, active_shadows: tuple[Any, ...]) -> WorkspaceState | None:
    if not context.workspace and not active_shadows:
        return None
    first_shadow = active_shadows[0].shadow_workspace if active_shadows else ""
    return WorkspaceState(
        workspace=context.workspace,
        shadow_workspace=first_shadow,
        shadow_workspaces=tuple(shadow.to_dict() for shadow in active_shadows),
    )


def _vault_handles(home: Path) -> tuple[Any, ...]:
    try:
        from .vault import VaultStore

        return VaultStore(home).list_handles(limit=100)
    except Exception:
        return ()


def _goal_facts(goal: Any) -> dict[str, Any]:
    return {
        "id": goal.id,
        "objective": goal.objective,
        "phase": goal.phase,
        "governance": goal.governance,
        "acceptance": goal.acceptance,
        "resolution": goal.resolution,
        "source": goal.source,
        "peer_id": goal.peer_id,
        "sender_id": goal.sender_id,
        "session_id": goal.session_id,
        "workspace": goal.workspace,
        "run_id": goal.run_id,
        "trace_id": goal.trace_id,
        "blocked_reason": goal.blocked_reason,
        "stop_condition": goal.stop_condition,
        "timeout": goal.timeout,
        "max_retries": goal.max_retries,
        "parent_goal_id": getattr(goal, "parent_goal_id", ""),
        "task_status": getattr(goal, "task_status", ""),
        "updated_at": goal.updated_at,
    }


def _workspace_matches(record_workspace: str, context_workspace: str) -> bool:
    if not record_workspace or not context_workspace:
        return True
    return record_workspace == context_workspace


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
