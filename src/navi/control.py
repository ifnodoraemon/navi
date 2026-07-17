from __future__ import annotations

import hashlib
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import connect
from .approval_contract import (
    APPROVAL_ACTION_CAPABILITY,
    APPROVAL_ACTION_RUN_EXECUTION,
    APPROVAL_ACTION_SESSION_ELEVATION,
    APPROVAL_DECISION_APPROVE,
    APPROVAL_DECISIONS,
    APPROVAL_STATUS_APPROVED,
    APPROVAL_STATUS_EXPIRED,
    APPROVAL_STATUS_PENDING,
    APPROVAL_STATUS_REJECTED,
)
from .lifecycle import Acceptance, Governance, Phase, Resolution
from .loop_contracts import BudgetState, LoopTerminalState, WorkspaceLock, WorkspaceState
from .loop_runs import LoopRunState
from .runs import Run, RunStore
from .runs.models import Approval
from .workspaces import ShadowWorkspaceManager, WorkspaceLockStore, workspaces_match


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
    recent_deliveries: tuple[dict[str, Any], ...] = ()
    recent_goal_outcomes: tuple[dict[str, Any], ...] = ()
    runtime_state_anomalies: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovalResolution:
    ok: bool
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
        active_run_count = runs.count_by_phases_scoped(
            [Phase.PENDING, Phase.RUNNING, Phase.PAUSED],
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            workspace=context.workspace,
        )
        normalized_decision = decision.strip().lower()
        if normalized_decision not in APPROVAL_DECISIONS:
            return _approval_not_resolved(
                decision=normalized_decision,
                reason="invalid_decision",
                code_present=bool(code),
                active_run_count=active_run_count,
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
            if approval is None:
                approval = runs.approval_by_code(
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
            if approval is None:
                approval = runs.approval_for_run(
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
                active_run_count=active_run_count,
                selection=selection,
                run_id=run_id,
            )

        if approval.status != APPROVAL_STATUS_PENDING:
            return self._resolve_existing_decision(
                runs=runs,
                approval=approval,
                decision=normalized_decision,
                selection=selection,
                code_present=bool(code),
                active_run_count=active_run_count,
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
                    active_run_count=active_run_count,
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
                    result_summary = f"approval_continuation_ready:{resolved.id}"
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
                resolution = Resolution.CANCELED
                runs.update_run_in_transaction(
                    conn,
                    resolved.run_id,
                    phase=phase,
                    governance=governance,
                    acceptance=acceptance,
                    resolution=resolution,
                    result_summary="approval_rejected",
                    error="",
                )
                status = APPROVAL_STATUS_REJECTED

        facts = self._resolution_facts(
            resolved=resolved,
            normalized_decision=normalized_decision,
            selection=selection,
            code_present=bool(code),
            active_run_count=active_run_count,
            status=status,
            phase=phase,
            governance=governance,
            acceptance=acceptance,
            resolution=resolution,
            state_transition="resolved",
        )
        if status == APPROVAL_STATUS_REJECTED:
            facts.update(self._reject_related_goal_loop(resolved))
        return ApprovalResolution(
            ok=True,
            facts=facts,
        )

    async def resolve_and_continue(
        self,
        *,
        decision: str,
        selection: str,
        context: SurfaceContext,
        runtime: Any | None,
        code: str = "",
        run_id: str = "",
        trace_id: str = "",
        event_bus: Any | None = None,
    ) -> ApprovalResolution:
        """Resolve one approval and resume its exact durable loop checkpoint.

        Approval is not completion evidence.  For capability/run execution
        approvals this method returns success only after the original loop has
        resumed and its own checker has produced the resulting durable state.
        """
        resolved = self.resolve(
            decision=decision,
            selection=selection,
            context=context,
            code=code,
            run_id=run_id,
        )
        if not resolved.ok or resolved.facts.get("status") != APPROVAL_STATUS_APPROVED:
            return resolved

        action = str(resolved.facts.get("action") or "")
        if action == APPROVAL_ACTION_SESSION_ELEVATION:
            facts = {**resolved.facts, "continuation_status": "not_applicable"}
            return ApprovalResolution(
                ok=True,
                facts=facts,
            )
        if action not in {APPROVAL_ACTION_CAPABILITY, APPROVAL_ACTION_RUN_EXECUTION}:
            facts = {
                **resolved.facts,
                "continuation_status": "not_applicable",
                "completion_evidence": False,
            }
            return ApprovalResolution(
                ok=True,
                facts=facts,
            )

        loop_context = self._related_goal_loop(str(resolved.facts.get("run_id") or ""))
        if not loop_context:
            facts = {
                **resolved.facts,
                "continuation_status": "unavailable",
                "completion_evidence": False,
                "reason": "approved_run_has_no_durable_loop",
            }
            return ApprovalResolution(
                ok=True,
                facts=facts,
            )

        goal, loop_run = loop_context
        if str(loop_run.terminal_state) == str(LoopTerminalState.WAITING_APPROVAL):
            current_approval = self._current_gate_approval(
                runs=RunStore(self.home),
                loop_run=loop_run,
                context=context,
            )
            if current_approval is None or current_approval.id != str(
                resolved.facts.get("approval_id") or ""
            ):
                current_run = RunStore(self.home).get(goal.run_id)
                facts = {
                    **resolved.facts,
                    "continuation_status": "waiting_approval",
                    "completion_evidence": False,
                    "loop_run_id": loop_run.run_id,
                    "loop_terminal_state": str(loop_run.terminal_state),
                    "reason": "approval_gate_superseded",
                }
                if current_approval is not None:
                    facts["current_approval"] = _approval_prompt_facts(current_approval)
                return ApprovalResolution(
                    ok=True,
                    facts=facts,
                )
        current_run = RunStore(self.home).get(goal.run_id)
        if (
            current_run is not None
            and current_run.phase == Phase.ENDED
            and str(loop_run.terminal_state) != str(LoopTerminalState.WAITING_APPROVAL)
        ):
            completion_evidence = (
                current_run.resolution == Resolution.SUCCESS
                and str(loop_run.terminal_state) == "converged"
            )
            facts = {
                **resolved.facts,
                "continuation_status": "completed",
                "completion_evidence": completion_evidence,
                "loop_run_id": loop_run.run_id,
                "loop_terminal_state": str(loop_run.terminal_state),
                "result_summary": current_run.result_summary,
                **_approval_continuation_facts(loop_run),
            }
            return ApprovalResolution(
                ok=True,
                facts=facts,
            )
        if runtime is None:
            facts = {
                **resolved.facts,
                "continuation_status": "queued",
                "completion_evidence": False,
                "loop_run_id": loop_run.run_id,
            }
            return ApprovalResolution(
                ok=True,
                facts=facts,
            )

        try:
            from .goal_state_graph import resume_goal_loop_run

            continued = await resume_goal_loop_run(
                home=self.home,
                loop_run_id=loop_run.run_id,
                runtime=runtime,
                trace_id=trace_id,
                input_text=context.input_text,
                event_bus=event_bus,
            )
        except Exception as exc:
            facts = {
                **resolved.facts,
                "continuation_status": "failed",
                "completion_evidence": False,
                "loop_run_id": loop_run.run_id,
                "continuation_error": f"{type(exc).__name__}: {exc}",
            }
            return ApprovalResolution(
                ok=False,
                facts=facts,
            )

        completion_evidence = bool(continued.to_facts().get("completion_evidence"))
        continuation_status = (
            "completed"
            if completion_evidence
            else str(continued.loop_run.terminal_state or "running")
        )
        facts = {
            **resolved.facts,
            "run_phase": str(continued.run.phase),
            "run_governance": str(continued.run.governance),
            "run_acceptance": str(continued.run.acceptance),
            "run_resolution": str(continued.run.resolution),
            "continuation_status": continuation_status,
            "completion_evidence": completion_evidence,
            "goal_id": continued.goal.id,
            "loop_run_id": continued.loop_run.run_id,
            "loop_terminal_state": str(continued.loop_run.terminal_state),
            "result_summary": continued.run.result_summary,
            **_approval_continuation_facts(continued.loop_run),
        }
        pending_approval = self._current_gate_approval(
            runs=RunStore(self.home),
            loop_run=continued.loop_run,
            context=context,
        )
        if pending_approval is not None:
            facts["pending_approval"] = _approval_prompt_facts(pending_approval)
        from .connector_delivery import connector_delivery_from_loop_result

        delivery = connector_delivery_from_loop_result(continued)
        if delivery is not None:
            facts["connector_delivery"] = delivery.to_dict()
        return ApprovalResolution(
            ok=True,
            facts=facts,
        )

    @staticmethod
    def _current_gate_approval(
        *,
        runs: RunStore,
        loop_run: LoopRunState,
        context: SurfaceContext,
    ) -> Approval | None:
        expected_id = _waiting_approval_id(loop_run.evidence)
        approval = runs.get_approval(expected_id) if expected_id else None
        if approval is None or not run_matches_context(approval, context):
            return None
        return approval

    def _resolve_existing_decision(
        self,
        *,
        runs: RunStore,
        approval: Approval,
        decision: str,
        selection: str,
        code_present: bool,
        active_run_count: int,
    ) -> ApprovalResolution:
        expected_status = (
            APPROVAL_STATUS_APPROVED
            if decision == APPROVAL_DECISION_APPROVE
            else APPROVAL_STATUS_REJECTED
        )
        if approval.status != expected_status:
            reason = (
                "approval_expired"
                if approval.status == APPROVAL_STATUS_EXPIRED
                else f"approval_already_{approval.status}"
            )
            return _approval_not_resolved(
                decision=decision,
                reason=reason,
                code_present=code_present,
                active_run_count=active_run_count,
                selection=selection,
                run_id=approval.run_id,
                approval_id=approval.id,
            )
        run = runs.get(approval.run_id)
        phase = run.phase if run is not None else Phase.ENDED
        governance = run.governance if run is not None else Governance.NONE
        acceptance = run.acceptance if run is not None else Acceptance.NONE
        resolution = run.resolution if run is not None else Resolution.NONE
        facts = self._resolution_facts(
            resolved=approval,
            normalized_decision=decision,
            selection=selection,
            code_present=code_present,
            active_run_count=active_run_count,
            status=approval.status,
            phase=phase,
            governance=governance,
            acceptance=acceptance,
            resolution=resolution,
            state_transition=f"already_{approval.status}",
        )
        return ApprovalResolution(
            ok=True,
            facts=facts,
        )

    @staticmethod
    def _resolution_facts(
        *,
        resolved: Approval,
        normalized_decision: str,
        selection: str,
        code_present: bool,
        active_run_count: int,
        status: str,
        phase: str,
        governance: str,
        acceptance: str,
        resolution: str,
        state_transition: str,
    ) -> dict[str, Any]:
        return {
            "decision": normalized_decision,
            "selection": selection,
            "code_present": code_present,
            "active_run_count": active_run_count,
            "run_id": resolved.run_id,
            "approval_id": resolved.id,
            "action": resolved.action,
            "status": status,
            "state_transition": state_transition,
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

    def _related_goal_loop(self, run_id: str):
        if not run_id:
            return None
        from .goals import GoalStore
        from .loop_runs import LoopRunStore

        goal = GoalStore(self.home).get_by_run(run_id)
        if goal is None:
            return None
        loop_runs = LoopRunStore(self.home).list_by_goal(goal.id, limit=1)
        if not loop_runs:
            return None
        return goal, loop_runs[0]

    def _reject_related_goal_loop(self, approval: Approval) -> dict[str, Any]:
        related = self._related_goal_loop(approval.run_id)
        if not related:
            return {
                "continuation_status": "rejected",
                "completion_evidence": False,
            }
        goal, loop_run = related
        from .goals import GoalStore
        from .loop_runs import LoopRunStore

        rejected_loop = LoopRunStore(self.home).reject_external_gate(
            loop_run.run_id,
            evidence={
                "approval_id": approval.id,
                "decision": APPROVAL_STATUS_REJECTED,
            },
        )
        rejected_goal = GoalStore(self.home).update_state(
            goal.id,
            phase=Phase.ENDED,
            governance=Governance.REJECTED,
            acceptance=Acceptance.REJECTED,
            resolution=Resolution.CANCELED,
            task_status="blocked",
            blocked_reason="approval_rejected",
            evidence={"approval_id": approval.id, "decision": "reject"},
            event_type="goal.approval_rejected",
        )
        return {
            "continuation_status": "rejected",
            "completion_evidence": False,
            "goal_id": rejected_goal.id if rejected_goal is not None else goal.id,
            "loop_run_id": rejected_loop.run_id,
            "loop_terminal_state": str(rejected_loop.terminal_state),
        }


class CurrentStateBuilder:
    def __init__(self, home: Path):
        self.home = home

    def build(self, context: SurfaceContext) -> CurrentState:
        runs = RunStore(self.home)
        active_runs = tuple(
            runs.list_by_phases_scoped(
                [Phase.PENDING, Phase.RUNNING, Phase.PAUSED],
                source=context.source,
                peer_id=context.peer_id,
                sender_id=context.sender_id,
                workspace=context.workspace,
                limit=100,
            )
        )
        pending_approvals = tuple(
            runs.list_approvals(
                status=APPROVAL_STATUS_PENDING,
                source=context.source,
                peer_id=context.peer_id,
                sender_id=context.sender_id,
                limit=100,
            )
        )
        active_goals = _active_goals(self.home, context)
        active_loop_runs = _active_loop_runs(self.home, active_goals)
        recent_goal_outcomes = _recent_goal_outcomes(self.home, context)
        delegation_state = _delegation_state(self.home, context)
        active_goal_run_ids = {str(goal.run_id) for goal in active_goals if goal.run_id}
        orphan_active_runs = [
            {"run_id": run.id, "title": run.title, "updated_at": run.updated_at}
            for run in active_runs
            if run.id not in active_goal_run_ids
        ]
        recent_deliveries = _recent_deliveries(self.home, context)
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
            delegation_state=delegation_state,
            recent_deliveries=recent_deliveries,
            recent_goal_outcomes=recent_goal_outcomes,
            runtime_state_anomalies={
                "active_run_without_active_goal_count": len(orphan_active_runs),
                "active_runs_without_active_goals": orphan_active_runs[:20],
            },
        )


def current_state_facts(state: CurrentState) -> dict[str, Any]:
    return {
        "current_time": current_time_facts(),
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
            _approval_prompt_facts(approval)
            for approval in state.pending_approvals
        ],
        "active_goals": [_goal_facts(goal) for goal in state.active_goals],
        "active_loop_runs": [_loop_run_prompt_facts(loop_run) for loop_run in state.active_loop_runs],
        "goal_state": {
            "active_goals": [_goal_facts(goal) for goal in state.active_goals],
        },
        "loop_run_state": {
            "active_loop_runs": [
                _loop_run_prompt_facts(loop_run) for loop_run in state.active_loop_runs
            ],
        },
        "approval_state": {
            "pending_approvals": [
                {
                    **_approval_prompt_facts(approval),
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
        "recent_deliveries": [dict(item) for item in state.recent_deliveries],
        "recent_goal_outcomes": [dict(item) for item in state.recent_goal_outcomes],
        "runtime_state_anomalies": dict(state.runtime_state_anomalies),
    }


def current_time_facts(*, now: float | None = None) -> dict[str, Any]:
    """Return the runtime clock as explicit model-facing facts."""
    timestamp = time.time() if now is None else now
    local_now = datetime.fromtimestamp(timestamp).astimezone()
    return {
        "unix": timestamp,
        "iso": local_now.isoformat(),
        "timezone": local_now.tzname() or "",
        "utc_offset": local_now.strftime("%z"),
    }


def _approval_prompt_facts(approval: Approval) -> dict[str, Any]:
    return {
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
        "args_digest": hashlib.sha256(approval.args_json.encode("utf-8")).hexdigest(),
        "expires_at": approval.expires_at,
        "created_at": approval.created_at,
        "updated_at": approval.updated_at,
        "reason": approval.reason,
    }


def _loop_run_prompt_facts(loop_run: LoopRunState) -> dict[str, Any]:
    return {
        "run_id": loop_run.run_id,
        "goal_id": loop_run.goal_id,
        "loop_spec_id": loop_run.loop_spec_id,
        "node": str(loop_run.node),
        "terminal_state": str(loop_run.terminal_state),
        "checkpoint_id": loop_run.checkpoint_id,
        "attempt": loop_run.attempt,
        "parent_run_id": loop_run.parent_run_id,
        "evidence_keys": sorted(loop_run.evidence),
        "updated_at": loop_run.updated_at,
    }


def _waiting_approval_id(evidence: dict[str, Any]) -> str:
    for container in (
        evidence,
        evidence.get("capability_result") if isinstance(evidence, dict) else None,
        evidence.get("executor") if isinstance(evidence, dict) else None,
    ):
        if not isinstance(container, dict):
            continue
        facts = container.get("facts")
        if not isinstance(facts, dict):
            continue
        approval = facts.get("approval")
        if isinstance(approval, dict) and str(approval.get("id") or ""):
            return str(approval["id"])
        entity_id = str(facts.get("entity_id") or "")
        if facts.get("entity_type") == "approval_request" and entity_id:
            return entity_id
    return ""


def _approval_continuation_facts(loop_run: LoopRunState) -> dict[str, Any]:
    """Expose the resumed operation's facts separately from approval metadata."""
    evidence = loop_run.evidence if isinstance(loop_run.evidence, dict) else {}
    executor = evidence.get("executor")
    checker_results = evidence.get("checker_results")
    facts: dict[str, Any] = {}
    if isinstance(executor, dict):
        facts["continuation_result"] = dict(executor)
    if isinstance(checker_results, list):
        facts["continuation_checker_results"] = [
            dict(item) for item in checker_results if isinstance(item, dict)
        ]
    return facts


def _active_goals(home: Path, context: SurfaceContext) -> tuple[Any, ...]:
    from .goals import GoalStore

    return tuple(
        GoalStore(home).list_scoped(
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            workspace=context.workspace,
            phases=(Phase.PENDING, Phase.RUNNING, Phase.PAUSED),
            limit=100,
        )
    )


def _recent_deliveries(home: Path, context: SurfaceContext) -> tuple[dict[str, Any], ...]:
    from .goals import GoalStore

    return tuple(
        GoalStore(home).list_recent_deliveries(
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            limit=20,
        )
    )


def _recent_goal_outcomes(
    home: Path,
    context: SurfaceContext,
    *,
    limit: int = 8,
) -> tuple[dict[str, Any], ...]:
    from .goals import GoalStore
    from .loop_runs import LoopRunStore

    goals = GoalStore(home).list_scoped(
        source=context.source,
        peer_id=context.peer_id,
        sender_id=context.sender_id,
        workspace=context.workspace,
        limit=limit,
    )
    runs = RunStore(home)
    loop_runs = LoopRunStore(home)
    outcomes: list[dict[str, Any]] = []
    for goal in goals:
        run = runs.get(goal.run_id) if goal.run_id else None
        goal_loop_runs = loop_runs.list_by_goal(goal.id, limit=1)
        loop_run = goal_loop_runs[0] if goal_loop_runs else None
        continuation = _approval_continuation_facts(loop_run) if loop_run else {}
        outcomes.append(
            {
                "goal_id": goal.id,
                "parent_goal_id": getattr(goal, "parent_goal_id", ""),
                "objective": goal.objective,
                "phase": goal.phase,
                "resolution": goal.resolution,
                "task_status": getattr(goal, "task_status", ""),
                "run_id": goal.run_id,
                "result_summary": _non_authoritative_run_summary(
                    str(run.result_summary or "") if run else ""
                ),
                "result_summary_provenance": "assistant_candidate_non_authoritative",
                "error": str(run.error or "") if run else "",
                "loop_terminal_state": str(loop_run.terminal_state or "")
                if loop_run
                else "",
                "continuation": _bounded_state_value(continuation),
                "updated_at": goal.updated_at,
            }
        )
    return tuple(outcomes)


def _delegation_state(home: Path, context: SurfaceContext) -> dict[str, Any]:
    from .goals import GoalStore

    children = GoalStore(home).list_scoped(
        source=context.source,
        peer_id=context.peer_id,
        sender_id=context.sender_id,
        workspace=context.workspace,
        child=True,
        limit=20,
    )
    child_count = GoalStore(home).count_scoped(
        source=context.source,
        peer_id=context.peer_id,
        sender_id=context.sender_id,
        workspace=context.workspace,
        child=True,
    )
    active_child_count = GoalStore(home).count_scoped(
        source=context.source,
        peer_id=context.peer_id,
        sender_id=context.sender_id,
        workspace=context.workspace,
        phases=(Phase.PENDING, Phase.RUNNING, Phase.PAUSED),
        child=True,
    )
    return {
        "child_count": child_count,
        "returned_child_count": len(children),
        "active_child_count": active_child_count,
        "children": [
            {
                "child_goal_id": goal.id,
                "parent_goal_id": goal.parent_goal_id,
                "objective": goal.objective,
                "phase": goal.phase,
                "resolution": goal.resolution,
                "task_status": goal.task_status,
                "updated_at": goal.updated_at,
            }
            for goal in children
        ],
    }


def _bounded_state_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 3:
        if isinstance(value, (dict, list, tuple)):
            return {"truncated": True, "type": type(value).__name__}
        return value
    if isinstance(value, str):
        return value if len(value) <= 500 else value[:500] + " ... [truncated]"
    if isinstance(value, dict):
        return {
            str(key): _bounded_state_value(nested, depth=depth + 1)
            for key, nested in list(value.items())[:30]
        }
    if isinstance(value, (list, tuple)):
        return [_bounded_state_value(item, depth=depth + 1) for item in value[:10]]
    return value


def _active_loop_runs(home: Path, active_goals: tuple[Any, ...]) -> tuple[LoopRunState, ...]:
    if not active_goals:
        return ()
    from .loop_runs import LoopRunStore

    goal_ids = {str(goal.id) for goal in active_goals}
    return tuple(LoopRunStore(home).list_current_for_goals(goal_ids, limit=100))


def _active_shadow_workspaces(home: Path, context: SurfaceContext) -> tuple[Any, ...]:
    try:
        shadows = ShadowWorkspaceManager(home).list_shadows(
            status="active",
            real_workspace=context.workspace,
            limit=100,
        )
    except Exception:
        return ()
    return shadows


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


def _workspace_matches(record_workspace: str, context_workspace: str, *, home: Path | None = None) -> bool:
    if not record_workspace or not context_workspace:
        return True
    if home is not None:
        return workspaces_match(home, record_workspace, context_workspace)
    return Path(record_workspace).expanduser().resolve() == Path(context_workspace).expanduser().resolve()


def run_matches_context(record: Any, context: Any) -> bool:
    record_sender = getattr(record, "sender_id", "")
    record_peer = getattr(record, "peer_id", "")
    record_source = getattr(record, "source", "")
    record_workspace = getattr(record, "workspace", "")
    context_workspace = getattr(context, "workspace", "")
    context_home = getattr(context, "home", None)
    if record_sender and context.sender_id and record_sender != context.sender_id:
        return False
    if record_peer and context.peer_id and record_peer != context.peer_id:
        return False
    if record_source and context.source and record_source != context.source:
        return False
    if not _workspace_matches(record_workspace, context_workspace, home=context_home):
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
        facts=facts,
    )
