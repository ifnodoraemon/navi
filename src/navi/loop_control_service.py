from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from .cron import next_cron_time, validate_cron
from .goals import Goal, GoalStore
from .lifecycle import Acceptance, Governance, Phase, Resolution
from .lifecycle_saga import LifecycleSagaStore
from .loop_contracts import (
    BudgetPolicy,
    CheckpointPolicy,
    EscalationPolicy,
    GoalSpec,
    LoopSpec,
    LoopTerminalState,
    RetryPolicy,
    RollbackPolicy,
    StateTransition,
    TimeoutPolicy,
    VerificationKind,
    VerificationStep,
    WorkspacePolicy,
)
from .loop_runs import LoopRunState, LoopRunStore
from .runs import Run, RunStore
from .state_graph import StateGraphRunResult


class ScheduleConflict(ValueError):
    """Raised when a recurring schedule operation would create ambiguity."""

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        cron_schedule: str,
        conflict_goal: Goal,
        allow_duplicate_schedule: bool,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.cron_schedule = cron_schedule
        self.conflict_goal = conflict_goal
        self.allow_duplicate_schedule = allow_duplicate_schedule

    def to_facts(self) -> dict[str, Any]:
        return {
            "entity_type": "goal_schedule_conflict",
            "entity_id": self.conflict_goal.id,
            "state_transition": "conflict",
            "turn_scope": "current",
            "operation": self.operation,
            "reason": "active_actor_cron_schedule_conflict",
            "cron_schedule": self.cron_schedule,
            "conflict_goal_id": self.conflict_goal.id,
            "conflict_goal": {
                "id": self.conflict_goal.id,
                "objective": self.conflict_goal.objective,
                "phase": self.conflict_goal.phase,
                "task_status": self.conflict_goal.task_status,
                "cron_schedule": self.conflict_goal.cron_schedule,
                "next_run_at": self.conflict_goal.next_run_at,
            },
            "allow_duplicate_schedule": self.allow_duplicate_schedule,
        }


@dataclass(frozen=True)
class OpenGoalRequest:
    objective: str
    workspace: str
    loop_kind: str = "durable_goal"
    source: str = "local"
    peer_id: str = ""
    sender_id: str = ""
    session_id: str = ""
    scope: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    permission_ceiling: str = "write"
    allowed_capabilities: tuple[str, ...] = ()
    verification_command: str = ""
    timeout_seconds: int = 120
    token_budget: int = 0
    call_budget: int = 0
    cost_budget: float = 0.0
    qps_limit: int = 0
    max_concurrent: int = 1
    auto_start: bool = True
    execution_mode: str = ""
    cron_schedule: str = ""
    parent_goal_id: str = ""
    trigger_facts: dict[str, Any] = field(default_factory=dict)
    task_context: dict[str, Any] = field(default_factory=dict)
    allow_duplicate_schedule: bool = False
    child_active_limit: int = 0


@dataclass(frozen=True)
class UpdateGoalRequest:
    goal_id: str
    objective: str = ""
    cron_schedule: str = ""
    scope: tuple[str, ...] | None = None
    constraints: tuple[str, ...] | None = None
    acceptance_criteria: tuple[str, ...] | None = None
    permission_ceiling: str = ""
    allowed_capabilities: tuple[str, ...] | None = None
    verification_command: str | None = None
    timeout_seconds: int = 0
    token_budget: int = -1
    call_budget: int = -1
    cost_budget: float = -1.0
    qps_limit: int = -1
    max_concurrent: int = 0
    allow_duplicate_schedule: bool = False


@dataclass(frozen=True)
class LoopControlServiceResult:
    goal: Goal
    run: Run
    loop_spec: LoopSpec
    loop_run: LoopRunState
    state_graph_result: StateGraphRunResult | None = None
    state_transition: str = "opened"

    def to_facts(self) -> dict[str, Any]:
        return {
            "entity_type": "goal",
            "entity_id": self.goal.id,
            "state_transition": self.state_transition,
            "turn_scope": "current",
            "goal_id": self.goal.id,
            "run_id": self.run.id,
            "loop_spec_id": self.loop_spec.id,
            "loop_run_id": self.loop_run.run_id,
            "route": "unified_loop",
            "loop_kind": str((self.loop_spec.goal.metadata or {}).get("loop_kind") or ""),
            "execution_mode": str((self.loop_spec.goal.metadata or {}).get("execution_mode") or ""),
            "cron_schedule": self.goal.cron_schedule,
            "next_run_at": self.goal.next_run_at,
            "registration_evidence": bool(self.goal.cron_schedule and self.goal.next_run_at > 0),
            "budget_policy": self.loop_spec.budget_policy.to_dict(),
            "phase": self.run.phase,
            "governance": self.run.governance,
            "acceptance": self.run.acceptance,
            "resolution": self.run.resolution,
            "loop_node": str(self.loop_run.node),
            "loop_terminal_state": str(self.loop_run.terminal_state),
            "completion_evidence": (
                self.loop_run.terminal_state == LoopTerminalState.CONVERGED
                and self.run.resolution == Resolution.SUCCESS
            ),
            "state_graph_result": self.state_graph_result.to_facts()
            if self.state_graph_result
            else {},
        }


class LoopControlService:
    """Root unified loop controller: Goal operation -> LoopSpec -> StateGraph."""

    def __init__(self, home: Path):
        self.home = home
        self.runs = RunStore(home)
        self.goals = GoalStore(home)
        self.loop_runs = LoopRunStore(home)
        self.lifecycle_sagas = LifecycleSagaStore(home)

    def open_goal(self, request: OpenGoalRequest) -> LoopControlServiceResult:
        objective = request.objective.strip()
        if not objective:
            raise ValueError("OpenGoalRequest.objective is required")
        loop_kind = _loop_kind(request.loop_kind)
        execution_mode = _execution_mode(request, loop_kind=loop_kind)
        cron_schedule = request.cron_schedule.strip()
        if loop_kind == "scheduled":
            if not cron_schedule:
                raise ValueError("scheduled goal requires cron_schedule")
            validate_cron(cron_schedule)
            existing = self.goals.find_active_cron_goal(
                objective=objective,
                cron_schedule=cron_schedule,
                source=request.source,
                peer_id=request.peer_id,
                sender_id=request.sender_id,
            )
            if existing is not None:
                existing_run = self.runs.get(existing.run_id)
                existing_loop_runs = self.loop_runs.list_by_goal(existing.id, limit=1)
                if existing_run is not None and existing_loop_runs:
                    existing_loop = existing_loop_runs[0]
                    return LoopControlServiceResult(
                        goal=existing,
                        run=existing_run,
                        loop_spec=_loop_spec_from_json(
                            self.loop_runs.get_spec_json(existing_loop.loop_spec_id)
                        ),
                        loop_run=existing_loop,
                        state_transition="existing",
                    )
            if not request.allow_duplicate_schedule:
                conflict = self.goals.find_active_cron_goal_by_schedule(
                    cron_schedule=cron_schedule,
                    source=request.source,
                    peer_id=request.peer_id,
                    sender_id=request.sender_id,
                )
                if conflict is not None:
                    raise ScheduleConflict(
                        "active scheduled goal already exists for this actor and "
                        "cron_schedule.",
                        operation="goal.open",
                        cron_schedule=cron_schedule,
                        conflict_goal=conflict,
                        allow_duplicate_schedule=request.allow_duplicate_schedule,
                    )
        workspace = _resolve_workspace(request.workspace)
        run = self.runs.create(
            title=objective[:120],
            prompt=objective,
            kind=f"loop:{loop_kind}",
            source=request.source,
            peer_id=request.peer_id,
            sender_id=request.sender_id,
            workspace=workspace,
            phase=Phase.RUNNING,
            governance=Governance.APPROVED,
            acceptance=Acceptance.NONE,
            resolution=Resolution.NONE,
            why_now=f"trigger=unified_loop loop_kind={loop_kind}",
        )
        goal: Goal | None = None
        try:
            next_run_at = next_cron_time(cron_schedule, now=time.time()) if cron_schedule else 0.0
            goal = self.goals.create(
                objective=objective,
                workspace=workspace,
                source=request.source,
                peer_id=request.peer_id,
                sender_id=request.sender_id,
                session_id=request.session_id,
                run_id=run.id,
                timeout=float(max(1, request.timeout_seconds)),
                max_retries=1,
                parent_goal_id=request.parent_goal_id,
                task_status="scheduled" if loop_kind == "scheduled" else "in_progress",
                cron_schedule=cron_schedule,
                next_run_at=next_run_at,
                child_active_limit=request.child_active_limit,
                evidence={
                    "route": "unified_loop",
                    "loop_kind": loop_kind,
                    "execution_mode": execution_mode,
                    "run_id": run.id,
                    "trigger_facts": dict(request.trigger_facts),
                    "verification_command_declared": bool(request.verification_command.strip()),
                },
            )
            spec = self._loop_spec_for_goal(goal, request, workspace=workspace)
            if loop_kind == "scheduled":
                registration = {
                    "state_transition": "schedule_registered",
                    "cron_schedule": cron_schedule,
                    "next_run_at": next_run_at,
                }
                loop_run = self.loop_runs.create_run(
                    spec,
                    node="evaluate",
                    terminal_state=LoopTerminalState.CONVERGED,
                    evidence=registration,
                    event_type="loop.schedule_registered",
                )
                updated_run = self.runs.update_run(
                    run.id,
                    phase=Phase.ENDED,
                    governance=Governance.NONE,
                    acceptance=Acceptance.ACCEPTED,
                    resolution=Resolution.SUCCESS,
                    result_summary="schedule registered",
                    error="",
                )
                if updated_run is not None:
                    run = updated_run
                updated_goal = self.goals.update_state(
                    goal.id,
                    phase=Phase.RUNNING,
                    governance=Governance.APPROVED,
                    acceptance=Acceptance.ACCEPTED,
                    resolution=Resolution.NONE,
                    task_status="scheduled",
                    evidence=registration,
                    event_type="goal.schedule_registered",
                )
                if updated_goal is not None:
                    goal = updated_goal
            else:
                loop_run = self.loop_runs.create_run(
                    spec,
                    evidence={"execution_mode": execution_mode},
                )
        except Exception as exc:
            self._compensate_open_failure(run, goal=goal, error=exc)
            raise
        return LoopControlServiceResult(
            goal=goal,
            run=run,
            loop_spec=spec,
            loop_run=loop_run,
            state_transition="scheduled" if loop_kind == "scheduled" else "opened",
        )

    def update_goal(self, request: UpdateGoalRequest) -> LoopControlServiceResult:
        goal_id = request.goal_id.strip()
        if not goal_id:
            raise ValueError("UpdateGoalRequest.goal_id is required")
        goal = self.goals.get(goal_id)
        if goal is None:
            raise KeyError(f"goal not found: {goal_id}")
        if goal.phase == Phase.ENDED:
            raise ValueError(f"terminal goal cannot be updated: {goal_id}")
        if not goal.cron_schedule:
            raise ValueError("goal.update currently supports scheduled recurring goals")
        template_runs = self.loop_runs.list_by_goal(goal.id, limit=1)
        if not template_runs:
            raise ValueError(f"scheduled goal has no registration loop: {goal.id}")
        template_loop = template_runs[0]
        previous_spec = _loop_spec_from_json(
            self.loop_runs.get_spec_json(template_loop.loop_spec_id)
        )
        objective = request.objective.strip() or goal.objective
        cron_schedule = request.cron_schedule.strip() or goal.cron_schedule
        validate_cron(cron_schedule)
        if not request.allow_duplicate_schedule:
            conflict = self.goals.find_active_cron_goal_by_schedule(
                cron_schedule=cron_schedule,
                source=goal.source,
                peer_id=goal.peer_id,
                sender_id=goal.sender_id,
                exclude_goal_id=goal.id,
            )
            if conflict is not None:
                raise ScheduleConflict(
                    "another active scheduled goal already exists for this actor "
                    "and cron_schedule.",
                    operation="goal.update",
                    cron_schedule=cron_schedule,
                    conflict_goal=conflict,
                    allow_duplicate_schedule=request.allow_duplicate_schedule,
                )
        next_run_at = (
            next_cron_time(cron_schedule, now=time.time())
            if cron_schedule != goal.cron_schedule
            else goal.next_run_at
        )
        evidence = {
            "state_transition": "updated",
            "previous_objective": goal.objective,
            "objective": objective,
            "previous_cron_schedule": goal.cron_schedule,
            "cron_schedule": cron_schedule,
            "next_run_at": next_run_at,
        }
        updated_goal = self.goals.update_scheduled_template(
            goal.id,
            objective=objective,
            cron_schedule=cron_schedule,
            next_run_at=next_run_at,
            evidence=evidence,
        )
        if updated_goal is None:
            raise KeyError(f"goal not found: {goal.id}")
        run = self.runs.get(updated_goal.run_id)
        if run is None:
            raise KeyError(f"run not found for goal: {updated_goal.run_id}")
        verification_command = _verification_command_from_spec(previous_spec)
        update_request = OpenGoalRequest(
            objective=updated_goal.objective,
            workspace=updated_goal.workspace,
            loop_kind="scheduled",
            source=updated_goal.source,
            peer_id=updated_goal.peer_id,
            sender_id=updated_goal.sender_id,
            session_id=updated_goal.session_id,
            scope=request.scope if request.scope is not None else tuple(previous_spec.goal.scope),
            constraints=request.constraints
            if request.constraints is not None
            else tuple(previous_spec.goal.constraints),
            acceptance_criteria=request.acceptance_criteria
            if request.acceptance_criteria is not None
            else tuple(previous_spec.goal.acceptance_criteria),
            permission_ceiling=request.permission_ceiling or previous_spec.goal.permission_ceiling,
            allowed_capabilities=request.allowed_capabilities
            if request.allowed_capabilities is not None
            else tuple(previous_spec.allowed_capabilities),
            verification_command=(
                verification_command
                if request.verification_command is None
                else request.verification_command
            ),
            timeout_seconds=(
                _timeout_seconds_from_spec(previous_spec)
                if request.timeout_seconds <= 0
                else request.timeout_seconds
            ),
            token_budget=(
                previous_spec.budget_policy.token_budget
                if request.token_budget < 0
                else request.token_budget
            ),
            call_budget=(
                previous_spec.budget_policy.call_budget
                if request.call_budget < 0
                else request.call_budget
            ),
            cost_budget=(
                previous_spec.budget_policy.cost_budget
                if request.cost_budget < 0
                else request.cost_budget
            ),
            qps_limit=(
                previous_spec.budget_policy.qps_limit
                if request.qps_limit < 0
                else request.qps_limit
            ),
            max_concurrent=(
                previous_spec.budget_policy.max_concurrent
                if request.max_concurrent <= 0
                else request.max_concurrent
            ),
            auto_start=True,
            execution_mode="scheduled",
            cron_schedule=cron_schedule,
        )
        updated_spec = self._loop_spec_for_goal(
            updated_goal,
            update_request,
            workspace=updated_goal.workspace,
        )
        updated_spec = replace(
            updated_spec,
            id=template_loop.loop_spec_id,
            created_at=previous_spec.created_at,
        )
        self.loop_runs.save_spec(updated_spec)
        return LoopControlServiceResult(
            goal=updated_goal,
            run=run,
            loop_spec=updated_spec,
            loop_run=template_loop,
            state_transition="updated",
        )

    def open_scheduled_occurrence(self, goal: Goal) -> LoopControlServiceResult:
        if not goal.cron_schedule:
            raise ValueError("scheduled occurrence requires a cron goal")
        template_runs = self.loop_runs.list_by_goal(goal.id, limit=1)
        if not template_runs:
            raise ValueError("scheduled goal has no loop specification")
        template = _loop_spec_from_json(self.loop_runs.get_spec_json(template_runs[0].loop_spec_id))
        verification_command = next(
            (step.command for step in template.verification_ladder if step.command),
            "",
        )
        children = self.goals.list_children(goal.id, limit=5, newest=True)
        occurrence_number = self.goals.count_children(goal.id) + 1
        prior_occurrences = []
        for child in children:
            accepted_result = (
                self.goals.accepted_result_for_run(child.run_id) if child.run_id else {}
            )
            prior_occurrences.append(
                {
                    "goal_id": child.id,
                    "run_id": child.run_id,
                    "created_at": child.created_at,
                    "phase": child.phase,
                    "acceptance": child.acceptance,
                    "resolution": child.resolution,
                    "task_status": child.task_status,
                    "accepted_result_text": str(accepted_result.get("body") or ""),
                    "accepted_result": accepted_result,
                    "delivery": self.goals.latest_delivery(child.id),
                }
            )
        trigger_facts = {
            "type": "scheduled_occurrence",
            "schedule_goal_id": goal.id,
            "cron_schedule": goal.cron_schedule,
            "triggered_at": time.time(),
            "occurrence_number": occurrence_number,
            "prior_occurrences": prior_occurrences,
        }
        task_context = _lineage_task_context(
            lineage_id=goal.id,
            lineage_kind="recurring_goal",
            sequence_number=occurrence_number,
            authoritative_prior_items=prior_occurrences,
        )
        return self.open_goal(
            OpenGoalRequest(
                objective=goal.objective,
                workspace=goal.workspace,
                loop_kind="durable_goal",
                source=goal.source,
                peer_id=goal.peer_id,
                sender_id=goal.sender_id,
                session_id=goal.session_id,
                scope=template.goal.scope,
                constraints=template.goal.constraints,
                acceptance_criteria=template.goal.acceptance_criteria,
                permission_ceiling=template.goal.permission_ceiling,
                allowed_capabilities=template.allowed_capabilities,
                verification_command=verification_command,
                token_budget=template.budget_policy.token_budget,
                call_budget=template.budget_policy.call_budget,
                cost_budget=template.budget_policy.cost_budget,
                qps_limit=template.budget_policy.qps_limit,
                max_concurrent=template.budget_policy.max_concurrent,
                auto_start=False,
                execution_mode="background",
                parent_goal_id=goal.id,
                trigger_facts=trigger_facts,
                task_context=task_context,
            )
        )

    def _compensate_open_failure(
        self,
        run: Run,
        *,
        goal: Goal | None,
        error: Exception,
    ) -> None:
        failure = f"loop_open_failed:{type(error).__name__}"
        failed_run = self.runs.update_run(
            run.id,
            phase=Phase.ENDED,
            governance=Governance.NONE,
            acceptance=Acceptance.REJECTED,
            resolution=Resolution.FAILED,
            result_summary="loop creation failed",
            error=failure,
        )
        if goal is not None and failed_run is not None:
            self.goals.update_for_run(
                failed_run,
                evidence={
                    "state_transition": "open_compensated",
                    "error": failure,
                },
            )

    def resume_loop(self, *, loop_run_id: str, workspace: str) -> LoopControlServiceResult:
        state = self.loop_runs.get_run(loop_run_id)
        if state is None:
            raise KeyError(f"loop run not found: {loop_run_id}")
        if str(state.terminal_state) in {
            str(LoopTerminalState.PAUSED),
            str(LoopTerminalState.WAITING_APPROVAL),
        }:
            state = self.loop_runs.reopen_for_resume(loop_run_id)
        spec = _loop_spec_from_json(self.loop_runs.get_spec_json(state.loop_spec_id))
        goal = self.goals.get(state.goal_id)
        if goal is None:
            raise KeyError(f"goal not found for loop run: {state.goal_id}")
        run = self.runs.get(goal.run_id) if goal.run_id else None
        if run is None:
            raise KeyError(f"run not found for goal: {goal.run_id}")
        _resolve_workspace(workspace or goal.workspace)
        return LoopControlServiceResult(
            goal=goal,
            run=run,
            loop_spec=spec,
            loop_run=state,
            state_transition="resumed",
        )

    def apply_state_graph_result(
        self,
        base: LoopControlServiceResult,
        graph_result: StateGraphRunResult,
        *,
        state_transition: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> LoopControlServiceResult:
        merged_evidence = {
            "loop_run_id": graph_result.run_state.run_id,
            "loop_terminal_state": graph_result.terminal_state,
            "checker_report": graph_result.checker_report.to_dict()
            if graph_result.checker_report
            else {},
            **(evidence or {}),
        }
        saga = self.lifecycle_sagas.prepare(
            operation_key=(
                f"loop-result:{graph_result.run_state.run_id}:"
                f"{graph_result.run_state.version}:{graph_result.terminal_state}"
            ),
            run_id=base.run.id,
            goal_id=base.goal.id,
            run_updates=self._run_updates_for_loop(base.run.id, graph_result),
            goal_evidence=merged_evidence,
        )
        updated_run, goal = self.lifecycle_sagas.apply(saga)
        if (
            graph_result.terminal_state == str(LoopTerminalState.CONVERGED)
            and str(graph_result.run_state.evidence.get("execution_mode") or "") == "background"
        ):
            self.goals.record_result_delivery_outbox(
                run=updated_run,
                goal=goal,
                body=_surface_message_from_result(graph_result),
                body_provenance="state_graph.evidence.responded_message",
                channel=goal.source,
                trace_id=goal.trace_id or updated_run.id,
            )
        return LoopControlServiceResult(
            goal=goal,
            run=updated_run,
            loop_spec=base.loop_spec,
            loop_run=graph_result.run_state,
            state_graph_result=graph_result,
            state_transition=state_transition or base.state_transition,
        )

    def fail_state_graph_execution(
        self,
        base: LoopControlServiceResult,
        *,
        error: Exception,
        execution_owner: str,
    ) -> None:
        failure = f"state_graph_exception:{type(error).__name__}"
        failed_loop = self.loop_runs.fail_active_run(
            base.loop_run.run_id,
            lease_owner=execution_owner,
            evidence={"reason": failure, "error": str(error)},
        )
        saga = self.lifecycle_sagas.prepare(
            operation_key=(
                f"loop-exception:{failed_loop.run_id}:{failed_loop.version}"
            ),
            run_id=base.run.id,
            goal_id=base.goal.id,
            run_updates={
                "phase": Phase.ENDED,
                "governance": Governance.NONE,
                "acceptance": Acceptance.REJECTED,
                "resolution": Resolution.FAILED,
                "result_summary": "",
                "error": failure,
            },
            goal_evidence={
                "state_transition": "state_graph_execution_failed",
                "loop_run_id": base.loop_run.run_id,
                "error": failure,
            },
        )
        self.lifecycle_sagas.apply(saga)

    def resume_goal(self, *, goal_id: str, workspace: str = "") -> LoopControlServiceResult:
        goal = self.goals.get(goal_id)
        if goal is None:
            raise KeyError(f"goal not found: {goal_id}")
        loop_run = self._active_loop_for_goal(goal_id)
        if loop_run is None:
            raise ValueError(f"goal has no resumable loop run: {goal_id}")
        return self.resume_loop(
            loop_run_id=loop_run.run_id,
            workspace=workspace or goal.workspace,
        )

    def cancel_goal(self, *, goal_id: str, reason: str = "") -> LoopControlServiceResult:
        goal = self.goals.get(goal_id)
        if goal is None:
            raise KeyError(f"goal not found: {goal_id}")
        loop_run = self._active_loop_for_goal(goal_id)
        if loop_run is None:
            if goal.cron_schedule:
                return self._cancel_scheduled_goal(goal, reason=reason)
            raise ValueError(f"goal has no active loop run to cancel: {goal_id}")
        return self.cancel_loop(loop_run_id=loop_run.run_id, reason=reason)

    def _cancel_scheduled_goal(
        self,
        goal: Goal,
        *,
        reason: str,
    ) -> LoopControlServiceResult:
        template_runs = self.loop_runs.list_by_goal(goal.id, limit=1)
        if not template_runs:
            raise ValueError(f"scheduled goal has no registration loop: {goal.id}")
        loop_run = template_runs[0]
        spec = _loop_spec_from_json(self.loop_runs.get_spec_json(loop_run.loop_spec_id))
        run = self.runs.get(goal.run_id)
        if run is None:
            raise KeyError(f"run not found for goal: {goal.run_id}")
        evidence = {"reason": reason.strip() or "schedule_cancel_requested"}
        updated_run = self.runs.update_run(
            run.id,
            phase=Phase.ENDED,
            governance=Governance.NONE,
            acceptance=Acceptance.REJECTED,
            resolution=Resolution.CANCELED,
            result_summary="schedule cancelled",
            error="loop_cancelled",
        )
        if updated_run is None:
            raise KeyError(f"run not found: {run.id}")
        updated_goal = self.goals.update_state(
            goal.id,
            phase=Phase.ENDED,
            governance=Governance.NONE,
            acceptance=Acceptance.REJECTED,
            resolution=Resolution.CANCELED,
            task_status="blocked",
            evidence=evidence,
            event_type="goal.schedule_cancelled",
        )
        return LoopControlServiceResult(
            goal=updated_goal or goal,
            run=updated_run,
            loop_spec=spec,
            loop_run=loop_run,
            state_transition="cancelled",
        )

    def cancel_loop(self, *, loop_run_id: str, reason: str = "") -> LoopControlServiceResult:
        state = self.loop_runs.get_run(loop_run_id)
        if state is None:
            raise KeyError(f"loop run not found: {loop_run_id}")
        spec = _loop_spec_from_json(self.loop_runs.get_spec_json(state.loop_spec_id))
        goal = self.goals.get(state.goal_id)
        if goal is None:
            raise KeyError(f"goal not found for loop run: {state.goal_id}")
        run = self.runs.get(goal.run_id) if goal.run_id else None
        if run is None:
            raise KeyError(f"run not found for goal: {goal.run_id}")
        if str(state.terminal_state) in {
            str(LoopTerminalState.PAUSED),
            str(LoopTerminalState.WAITING_APPROVAL),
        }:
            evidence = {"reason": reason.strip() or "cancel_requested"}
            cancelled = self.loop_runs.cancel_external_wait(
                state.run_id,
                evidence=evidence,
            )
        elif state.is_terminal():
            return LoopControlServiceResult(
                goal=goal,
                run=run,
                loop_spec=spec,
                loop_run=state,
                state_transition="already_terminal",
            )
        else:
            evidence = {"reason": reason.strip() or "cancel_requested"}
            checkpoint = self.loop_runs.write_checkpoint(
                state.run_id,
                node=state.node,
                inputs={"control": "cancel", **evidence},
                state=state.to_dict(),
            )
            cancelled = self.loop_runs.transition(
                state.run_id,
                node=state.node,
                checkpoint_id=checkpoint.id,
                terminal_state=LoopTerminalState.CANCELLED,
                condition="cancel_requested",
                evidence=evidence,
            )
        updated_run = self.runs.update_run(
            run.id,
            phase=Phase.ENDED,
            governance=Governance.NONE,
            acceptance=Acceptance.REJECTED,
            resolution=Resolution.CANCELED,
            result_summary="loop cancelled",
            error="loop_cancelled",
        )
        if updated_run is None:
            raise KeyError(f"run not found: {run.id}")
        updated_goal = self.goals.update_state(
            goal.id,
            phase=Phase.ENDED,
            governance=Governance.NONE,
            acceptance=Acceptance.REJECTED,
            resolution=Resolution.CANCELED,
            task_status="blocked",
            evidence={"loop_run_id": cancelled.run_id, **evidence},
            event_type="goal.cancelled",
        )
        return LoopControlServiceResult(
            goal=updated_goal or goal,
            run=updated_run,
            loop_spec=spec,
            loop_run=cancelled,
            state_transition="cancelled",
        )

    def goal_state(
        self,
        *,
        goal_id: str = "",
        loop_run_id: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        if loop_run_id:
            loop_run = self.loop_runs.get_run(loop_run_id)
            if loop_run is None:
                raise KeyError(f"loop run not found: {loop_run_id}")
            goal = self.goals.get(loop_run.goal_id)
            run = self.runs.get(goal.run_id) if goal and goal.run_id else None
            return {
                "entity_type": "goal",
                "entity_id": goal.id if goal else loop_run.goal_id,
                "state_transition": "state_read",
                "turn_scope": "current",
                "goal": asdict(goal) if goal else {},
                "run": asdict(run) if run else {},
                "loop_run": loop_run.to_dict(),
            }
        if goal_id:
            goal = self.goals.get(goal_id)
            if goal is None:
                raise KeyError(f"goal not found: {goal_id}")
            run = self.runs.get(goal.run_id) if goal.run_id else None
            loop_runs = self.loop_runs.list_by_goal(goal_id, limit=limit)
            return {
                "entity_type": "goal",
                "entity_id": goal.id,
                "state_transition": "state_read",
                "turn_scope": "current",
                "goal": asdict(goal),
                "run": asdict(run) if run else {},
                "loop_runs": [item.to_dict() for item in loop_runs],
            }
        active = self.loop_runs.list_active(limit=limit)
        active_goals = [self.goals.get(item.goal_id) for item in active]
        return {
            "entity_type": "goal",
            "entity_id": "",
            "state_transition": "state_read",
            "turn_scope": "current",
            "active_loop_runs": [item.to_dict() for item in active],
            "active_goals": [asdict(goal) for goal in active_goals if goal is not None],
        }

    def goal_loop_spec(self, goal_id: str) -> LoopSpec:
        """Return the persisted policy envelope for a goal."""
        goal = self.goals.get(goal_id)
        if goal is None:
            raise KeyError(f"goal not found: {goal_id}")
        loop_runs = self.loop_runs.list_by_goal(goal_id, limit=1)
        if not loop_runs:
            raise KeyError(f"loop specification not found for goal: {goal_id}")
        return _loop_spec_from_json(self.loop_runs.get_spec_json(loop_runs[0].loop_spec_id))

    def _loop_spec_for_goal(
        self,
        goal: Goal,
        request: OpenGoalRequest,
        *,
        workspace: str,
    ) -> LoopSpec:
        timeout = TimeoutPolicy(seconds=float(max(1, request.timeout_seconds)))
        command = request.verification_command.strip()
        if command:
            verification_ladder = (
                VerificationStep(
                    kind=VerificationKind.COMMAND_EXIT_CODE,
                    name="verification_command",
                    command=command,
                    timeout=timeout,
                ),
            )
        else:
            verification_ladder = (
                VerificationStep(
                    kind=VerificationKind.LLM_CHECKER,
                    name="objective_check",
                    evidence_key="semantic_checker_result",
                    timeout=timeout,
                    required=True,
                ),
            )
        allowed = request.allowed_capabilities or ("*",)
        goal_spec = GoalSpec(
            objective=goal.objective,
            scope=request.scope or (f"repo:{workspace}",),
            constraints=request.constraints,
            acceptance_criteria=request.acceptance_criteria,
            permission_ceiling=request.permission_ceiling or "write",
            owner=request.sender_id or request.peer_id,
            metadata={
                "goal_id": goal.id,
                "run_id": goal.run_id,
                "session_id": goal.session_id,
                "route": "unified_loop",
                "loop_kind": _loop_kind(request.loop_kind),
                "execution_mode": _execution_mode(
                    request,
                    loop_kind=_loop_kind(request.loop_kind),
                ),
                "source": goal.source,
                "peer_id": goal.peer_id,
                "sender_id": goal.sender_id,
                "parent_goal_id": goal.parent_goal_id,
                "workspace": goal.workspace,
                "trigger_facts": dict(request.trigger_facts),
                "task_context": _task_context_for_request(goal, request),
                "execution_profile": _execution_profile(loop_kind=_loop_kind(request.loop_kind)),
            },
        )
        spec = LoopSpec.from_goal(
            goal_spec,
            goal_id=goal.id,
            allowed_capabilities=allowed,
            verification_ladder=verification_ladder,
        )
        spec = replace(
            spec,
            budget_policy=BudgetPolicy(
                token_budget=max(0, int(request.token_budget)),
                call_budget=max(0, int(request.call_budget)),
                cost_budget=max(0.0, float(request.cost_budget)),
                qps_limit=max(0, int(request.qps_limit)),
                max_concurrent=max(1, int(request.max_concurrent)),
            ),
        )
        spec.validate()
        return spec

    def _run_updates_for_loop(
        self,
        run_id: str,
        result: StateGraphRunResult,
    ) -> dict[str, Any]:
        terminal = result.terminal_state
        current = self.runs.get(run_id)
        existing_summary = current.result_summary if current is not None else ""
        surface_message = _surface_message_from_result(result)
        if terminal == str(LoopTerminalState.CONVERGED):
            return {
                "phase": Phase.ENDED,
                "governance": Governance.NONE,
                "acceptance": Acceptance.ACCEPTED,
                "resolution": Resolution.SUCCESS,
                "result_summary": surface_message,
                "error": "",
            }
        elif terminal == str(LoopTerminalState.PAUSED):
            return {
                "phase": Phase.PAUSED,
                "acceptance": Acceptance.UNVERIFIED,
                "resolution": Resolution.BLOCKED,
                "result_summary": existing_summary or surface_message,
                "error": "",
            }
        elif terminal == str(LoopTerminalState.WAITING_APPROVAL):
            return {
                "phase": Phase.PAUSED,
                "governance": Governance.AWAITING_APPROVAL,
                "acceptance": Acceptance.UNVERIFIED,
                "resolution": Resolution.BLOCKED,
                "result_summary": existing_summary or surface_message,
                "error": "",
            }
        elif terminal == str(LoopTerminalState.CONFLICTED):
            return {
                "phase": Phase.PAUSED,
                "governance": Governance.AWAITING_APPROVAL,
                "acceptance": Acceptance.UNVERIFIED,
                "resolution": Resolution.BLOCKED,
                "result_summary": surface_message,
                "error": "loop_conflicted",
            }
        elif terminal == str(LoopTerminalState.BLOCKED):
            return {
                "phase": Phase.ENDED,
                "governance": Governance.NONE,
                "acceptance": Acceptance.REJECTED,
                "resolution": Resolution.BLOCKED,
                "result_summary": surface_message,
                "error": "loop_blocked",
            }
        elif terminal == str(LoopTerminalState.TIMED_OUT):
            return {
                "phase": Phase.ENDED,
                "governance": Governance.NONE,
                "acceptance": Acceptance.REJECTED,
                "resolution": Resolution.FAILED,
                "result_summary": surface_message,
                "error": "loop_timed_out",
            }
        elif terminal in {
            str(LoopTerminalState.CANCELLED),
            str(LoopTerminalState.SUPERSEDED),
        }:
            return {
                "phase": Phase.ENDED,
                "governance": Governance.NONE,
                "acceptance": Acceptance.REJECTED,
                "resolution": Resolution.CANCELED,
                "result_summary": surface_message,
                "error": f"loop_{terminal}",
            }
        elif terminal == str(LoopTerminalState.FAILED):
            return {
                "phase": Phase.ENDED,
                "governance": Governance.NONE,
                "acceptance": Acceptance.REJECTED,
                "resolution": Resolution.FAILED,
                "result_summary": surface_message,
                "error": "loop_failed",
            }
        elif terminal:
            return {
                "phase": Phase.ENDED,
                "governance": Governance.NONE,
                "acceptance": Acceptance.REJECTED,
                "resolution": Resolution.FAILED,
                "result_summary": surface_message,
                "error": f"loop_{terminal}",
            }
        return {
            "phase": Phase.RUNNING,
            "acceptance": Acceptance.UNVERIFIED,
            "resolution": Resolution.BLOCKED,
            "result_summary": surface_message,
            "error": "",
        }

    def _active_loop_for_goal(self, goal_id: str) -> LoopRunState | None:
        matches = self.loop_runs.list_by_goal_filtered(
            goal_id,
            terminal_states=(
                "",
                LoopTerminalState.PAUSED,
                LoopTerminalState.WAITING_APPROVAL,
            ),
            limit=1,
        )
        return matches[0] if matches else None


def _resolve_workspace(workspace: str) -> str:
    raw = workspace.strip()
    if not raw:
        raise ValueError("workspace is required")
    path = Path(raw).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        raise ValueError("workspace must be an existing directory")
    return str(path)


def _execution_profile(*, loop_kind: str) -> dict[str, Any]:
    if loop_kind == "turn":
        return {
            "name": "interactive_turn",
            "persistence": "transient_audit",
            "checker_tier": "semantic",
            "retention_seconds": 86_400,
        }
    if loop_kind == "control":
        return {
            "name": "control_operation",
            "persistence": "durable",
            "checker_tier": "objective_evidence",
            "retention_seconds": 604_800,
        }
    if loop_kind == "scheduled":
        return {
            "name": "scheduled_goal",
            "persistence": "durable",
            "checker_tier": "objective_evidence",
            "retention_seconds": 0,
        }
    return {
        "name": "durable_goal",
        "persistence": "durable",
        "checker_tier": "semantic",
        "retention_seconds": 0,
    }


def _lineage_task_context(
    *,
    lineage_id: str,
    lineage_kind: str,
    sequence_number: int,
    authoritative_prior_items: list[dict[str, Any]],
) -> dict[str, Any]:
    return _normalize_task_context(
        {
            "lineage": {
                "id": lineage_id,
                "kind": lineage_kind,
            },
            "progress": {
                "scope": "lineage",
                "sequence_number": sequence_number,
                "authority": "same_lineage_authoritative_prior_items",
                "authoritative_prior_items": authoritative_prior_items,
                "ambient_history_authoritative": False,
            },
        },
        current_goal_id="",
        parent_goal_id="",
    )


def _task_context_for_request(
    goal: Goal,
    request: OpenGoalRequest,
) -> dict[str, Any]:
    if request.task_context:
        return _normalize_task_context(
            request.task_context,
            current_goal_id=goal.id,
            parent_goal_id=goal.parent_goal_id,
        )
    return _normalize_task_context(
        {
            "lineage": {
                "id": goal.parent_goal_id or goal.id,
                "kind": "goal",
            },
            "progress": {
                "scope": "goal",
                "sequence_number": 0,
                "authority": "current_goal",
                "authoritative_prior_items": [],
                "ambient_history_authoritative": False,
            },
        },
        current_goal_id=goal.id,
        parent_goal_id=goal.parent_goal_id,
    )


def _normalize_task_context(
    raw: dict[str, Any],
    *,
    current_goal_id: str,
    parent_goal_id: str,
) -> dict[str, Any]:
    lineage = raw.get("lineage") if isinstance(raw, dict) else {}
    progress = raw.get("progress") if isinstance(raw, dict) else {}
    lineage_dict = dict(lineage) if isinstance(lineage, dict) else {}
    progress_dict = dict(progress) if isinstance(progress, dict) else {}
    authoritative_prior_items = [
        dict(item)
        for item in progress_dict.get("authoritative_prior_items") or []
        if isinstance(item, dict)
    ]
    return {
        "lineage": {
            "id": str(lineage_dict.get("id") or parent_goal_id or current_goal_id),
            "kind": str(lineage_dict.get("kind") or "goal"),
            "current_goal_id": current_goal_id,
            "parent_goal_id": parent_goal_id,
        },
        "progress": {
            "scope": str(progress_dict.get("scope") or "goal"),
            "sequence_number": _int_or_default(
                progress_dict.get("sequence_number"),
                0,
            ),
            "authority": str(progress_dict.get("authority") or "current_goal"),
            "authoritative_prior_items": authoritative_prior_items,
            "authoritative_prior_item_count": len(authoritative_prior_items),
            "ambient_history_authoritative": bool(
                progress_dict.get("ambient_history_authoritative", False)
            ),
        },
    }


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _verification_command_from_spec(spec: LoopSpec) -> str:
    return next((step.command for step in spec.verification_ladder if step.command), "")


def _timeout_seconds_from_spec(spec: LoopSpec) -> int:
    seconds = next(
        (step.timeout.seconds for step in spec.verification_ladder if step.timeout.seconds > 0),
        120.0,
    )
    return max(1, int(seconds))


def _execution_mode(request: OpenGoalRequest, *, loop_kind: str) -> str:
    declared = request.execution_mode.strip().lower()
    if declared:
        if declared not in {"foreground", "background", "manual", "scheduled"}:
            raise ValueError(f"unsupported execution_mode: {request.execution_mode}")
        if loop_kind == "scheduled" and declared != "scheduled":
            raise ValueError("scheduled goal registration requires execution_mode=scheduled")
        return declared
    if loop_kind == "scheduled":
        return "scheduled"
    return "background" if request.auto_start else "manual"


def _loop_kind(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if not normalized:
        return "durable_goal"
    if normalized not in {"turn", "control", "durable_goal", "scheduled"}:
        raise ValueError(f"unsupported loop_kind: {value}")
    return normalized


def _surface_message_from_result(result: StateGraphRunResult) -> str:
    evidence = result.evidence or {}
    responded = str(evidence.get("responded_message") or "").strip()
    if responded:
        return responded
    return ""


def _loop_spec_from_json(raw: str) -> LoopSpec:
    data = json.loads(raw)
    goal_data = data["goal"]
    spec = LoopSpec(
        id=str(data["id"]),
        goal_id=str(data["goal_id"]),
        goal=GoalSpec(
            objective=str(goal_data["objective"]),
            scope=tuple(str(item) for item in goal_data["scope"]),
            constraints=tuple(str(item) for item in goal_data["constraints"]),
            acceptance_criteria=tuple(str(item) for item in goal_data["acceptance_criteria"]),
            permission_ceiling=str(goal_data["permission_ceiling"]),
            risk_level=str(goal_data["risk_level"]),
            owner=str(goal_data["owner"]),
            resume_policy=str(goal_data["resume_policy"]),
            cancel_policy=str(goal_data["cancel_policy"]),
            memory_policy=str(goal_data["memory_policy"]),
            metadata=_required_dict(goal_data, "metadata"),
        ),
        state_graph=tuple(
            StateTransition(
                source=str(item["source"]),
                target=str(item["target"]),
                condition=str(item["condition"]),
            )
            for item in data["state_graph"]
        ),
        allowed_capabilities=tuple(str(item) for item in data["allowed_capabilities"]),
        verification_ladder=tuple(
            VerificationStep(
                kind=str(item["kind"]),
                name=str(item["name"]),
                required=bool(item["required"]),
                command=str(item["command"]),
                evidence_key=str(item["evidence_key"]),
                timeout=TimeoutPolicy(**_required_dict(item, "timeout")),
            )
            for item in data["verification_ladder"]
        ),
        workspace_policy=WorkspacePolicy(**_required_dict(data, "workspace_policy")),
        checkpoint_policy=CheckpointPolicy(**_required_dict(data, "checkpoint_policy")),
        retry_policy=RetryPolicy(**_required_dict(data, "retry_policy")),
        budget_policy=BudgetPolicy(**_required_dict(data, "budget_policy")),
        rollback_policy=RollbackPolicy(**_required_dict(data, "rollback_policy")),
        escalation_policy=EscalationPolicy(**_required_dict(data, "escalation_policy")),
        terminal_states=tuple(str(item) for item in data["terminal_states"]),
        created_at=float(data["created_at"]),
    )
    spec.validate()
    return spec


def _required_dict(container: dict[str, Any], key: str) -> dict[str, Any]:
    value = container[key]
    if not isinstance(value, dict):
        raise ValueError(f"LoopSpec.{key} must be an object")
    return value
