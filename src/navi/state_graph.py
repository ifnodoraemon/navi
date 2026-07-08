from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .capabilities import CapabilityRegistry
from .capabilities_types import CapabilityContext
from .checker import CheckerReport, DeterministicChecker
from .harness import Harness, HarnessCommand, HarnessResult
from .loop_contracts import (
    LoopNode,
    LoopRunState,
    LoopSpec,
    LoopTerminalState,
    LockMode,
    MergeStatus,
    ResourceDecision,
    VerificationKind,
    VerificationStep,
    WorkspaceMode,
)
from .loop_runs import LoopCheckpoint, LoopRunStore

from .runtime import AgentRuntime
from .syscalls import ModelSyscallPlanner
from .resource_gateway import GlobalResourceGateway, ResourceGrant, ResourceLimits, ResourceRequest
from .workspaces import LockAcquireResult


@dataclass(frozen=True)
class StateGraphRunResult:
    run_state: LoopRunState
    checker_report: CheckerReport | None = None
    resource_grants: tuple[ResourceGrant, ...] = ()
    harness_results: tuple[HarnessResult, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal_state(self) -> str:
        return str(self.run_state.terminal_state)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_state": self.run_state.to_dict(),
            "checker_report": self.checker_report.to_dict() if self.checker_report else {},
            "resource_grants": [grant.to_dict() for grant in self.resource_grants],
            "harness_results": [result.to_facts() for result in self.harness_results],
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class PlannedCapabilityStep:
    tool: str
    args: dict[str, Any]
    permission: str
    model_role: str = "executor"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "args": dict(self.args),
            "permission": self.permission,
            "model_role": self.model_role,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ExecutedCapabilityStep:
    ok: bool
    action: str
    facts: dict[str, Any]
    message: str = ""
    error_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "facts": dict(self.facts),
            "message": self.message,
            "error_reason": self.error_reason,
        }


@dataclass(frozen=True)
class ReflectionDecision:
    retry: bool
    reason_code: str
    facts: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "retry": self.retry,
            "reason_code": self.reason_code,
            "facts": dict(self.facts),
        }


class RecoveryReflectorPort:
    """Reflector node port that turns failed evidence into retry facts."""

    def reflect(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        checker_report: CheckerReport,
        evidence: dict[str, Any],
        harness_results: tuple[HarnessResult, ...],
    ) -> ReflectionDecision:
        reason_code = _checker_reason_code(checker_report)
        recovery_facts = {
            "trigger": "loop.check",
            "reason_code": reason_code,
            "blocked": True,
            "failure_domain": "checker_blocked",
            "loop_run_id": state.run_id,
            "attempt": state.attempt,
            "goal_id": spec.goal_id,
            "checker_report": checker_report.to_dict(),
            "harness_results": [item.to_facts() for item in harness_results],
        }
        return ReflectionDecision(
            retry=state.attempt < spec.retry_policy.max_attempts,
            reason_code=reason_code,
            facts={
                "recovery": recovery_facts,
                "recovery_fact": json.dumps(
                    {
                        "fact_type": "loop_checker_fact",
                        "facts": recovery_facts,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
            },
        )


class ModelCapabilityPlannerPort:
    """Planner node port for the durable StateGraph.

    It reuses the existing model syscall planner but constrains the exposed
    tool manifest to the LoopSpec's allowed capabilities.
    """

    def __init__(self, *, runtime: AgentRuntime, capabilities: CapabilityRegistry):
        self.runtime = runtime
        self.capabilities = capabilities
        self.planner = ModelSyscallPlanner(runtime.provider)

    async def plan(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        workspace: Path,
        evidence: dict[str, Any],
    ) -> PlannedCapabilityStep:
        allowed = set(spec.allowed_capabilities)
        context = CapabilityContext(
            home=self.capabilities.home,
            source="state_graph",
            peer_id="state_graph",
            sender_id=spec.goal.owner or "state_graph",
            permission_ceiling=spec.goal.permission_ceiling,
            workspace=str(workspace),
        )
        tools = [
            item
            for item in self.capabilities.planner_specs(
                permission_ceiling=spec.goal.permission_ceiling,
                context=context,
            )
            if item.name in allowed
        ]
        if not tools:
            return PlannedCapabilityStep(
                tool="system.planner_error",
                permission="read",
                args={"reason": "no_allowed_capabilities", "allowed_capabilities": sorted(allowed)},
                model_role="planner",
                reason="LoopSpec allowed no planner-visible capabilities",
            )
        syscalls = await self.planner.plan(
            spec.goal.objective,
            tools=tools,
            conversation_context="",
            runtime_facts={
                "loop_spec": spec.to_dict(),
                "loop_run_state": state.to_dict(),
                "objective_evidence": dict(evidence),
            },
            permission_ceiling=spec.goal.permission_ceiling,
            model_roles=self.runtime.model_roles(),
            durable_constraints=_goal_constraints(spec),
        )
        selected = syscalls[0]
        return PlannedCapabilityStep(
            tool=selected.tool,
            args=dict(selected.args),
            permission=selected.permission,
            model_role=selected.model_role,
            reason=selected.reason,
        )


class CapabilityExecutorPort:
    """Executor node port that invokes the capability registry in the node workspace."""

    def __init__(self, *, home: Path, context: CapabilityContext):
        self.home = home
        self.context = context

    async def execute(
        self,
        step: PlannedCapabilityStep,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        workspace: Path,
    ) -> ExecutedCapabilityStep:
        registry = CapabilityRegistry(
            home=self.home,
            project_dir=workspace,
            permission_ceiling=spec.goal.permission_ceiling,
            enforce_connector_source_policy=False,
            governed_run_id=state.run_id,
            sensitive_approval_mode="skip",
        )
        context = replace(
            self.context,
            workspace=str(workspace),
            permission_ceiling=spec.goal.permission_ceiling,
            source=self.context.source or "state_graph",
        )
        result = await registry.invoke(
            step.tool,
            step.args,
            permission=step.permission,
            context=context,
        )
        return ExecutedCapabilityStep(
            ok=result.ok,
            action=result.action,
            facts=result.facts or {},
            message=result.message,
            error_reason=result.error_reason,
        )


class DurableStateGraphRunner:
    """Durable PLAN -> EXECUTE -> EVALUATE runner for the Navi 2.0 control plane."""

    def __init__(
        self,
        *,
        home: Path,
        gateway: GlobalResourceGateway | None = None,
        harness: Harness | None = None,
        checker: DeterministicChecker | None = None,
        planner_port: ModelCapabilityPlannerPort | None = None,
        executor_port: CapabilityExecutorPort | None = None,
        reflector_port: RecoveryReflectorPort | None = None,
    ):
        self.home = home
        self.store = LoopRunStore(home)
        self.gateway = gateway or GlobalResourceGateway(ResourceLimits(max_concurrent=1))
        self.harness = harness or Harness(home=home)
        self.checker = checker or DeterministicChecker()
        self.planner_port = planner_port
        self.executor_port = executor_port
        self.reflector_port = reflector_port or RecoveryReflectorPort()

    def run(
        self,
        spec: LoopSpec,
        *,
        workspace: Path,
        run_id: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> StateGraphRunResult:
        raise RuntimeError(
            "DurableStateGraphRunner.run() is disabled. "
            "Use run_async() with explicit planner_port and executor_port."
        )

    async def run_async(
        self,
        spec: LoopSpec,
        *,
        workspace: Path,
        run_id: str = "",
        evidence: dict[str, Any] | None = None,
    ) -> StateGraphRunResult:
        if self.planner_port is None or self.executor_port is None:
            raise RuntimeError(
                "Durable StateGraph requires explicit planner_port and executor_port."
            )
        spec.validate()
        state = self.store.get_run(run_id) if run_id else None
        if state is None:
            state = self.store.create_run(spec)
        if state.is_terminal():
            return StateGraphRunResult(run_state=state, evidence=evidence or {})

        collected_evidence: dict[str, Any] = dict(evidence or {})
        grants: list[ResourceGrant] = []
        harness_results: list[HarnessResult] = []
        checker_report: CheckerReport | None = None
        planned_step: PlannedCapabilityStep | None = None
        execution_workspace = workspace
        shadow_workspace = None
        if str(spec.workspace_policy.mode) == str(WorkspaceMode.SHADOW):
            shadow_workspace = self.harness.create_shadow_workspace(
                run_id=state.run_id,
                workspace=workspace,
            )
            execution_workspace = Path(shadow_workspace.shadow_workspace)
            collected_evidence["shadow_workspace"] = shadow_workspace.to_dict()

        if state.node == LoopNode.PLAN:
            state, stopped, grant = self._gate_or_stop(state, kind="state_graph.plan")
            grants.append(grant)
            if stopped:
                return StateGraphRunResult(
                    run_state=state,
                    resource_grants=tuple(grants),
                    evidence=collected_evidence,
                )
            if self.planner_port is not None:
                self.store.write_checkpoint(
                    state.run_id,
                    node=LoopNode.PLAN,
                    inputs={"planner_node": "start"},
                    state=state.to_dict(),
                )
                planned_step = await self.planner_port.plan(
                    spec,
                    state,
                    workspace=execution_workspace,
                    evidence=collected_evidence,
                )
                collected_evidence["planned_capability"] = planned_step.to_dict()
                if planned_step.tool == "system.planner_error":
                    self._discard_shadow_if_needed(spec, state.run_id, shadow_workspace)
                    state = self._transition(
                        state,
                        node=LoopNode.PLAN,
                        condition="planner_failed",
                        terminal_state=LoopTerminalState.FAILED,
                        evidence=planned_step.to_dict(),
                    )
                    self.gateway.release()
                    return StateGraphRunResult(
                        run_state=state,
                        resource_grants=tuple(grants),
                        evidence=collected_evidence,
                    )
            state = self._transition(
                state,
                node=LoopNode.EXECUTE,
                condition="plan_ready",
                evidence={"planned_capability": planned_step.to_dict()},
            )
            self.gateway.release()

        if state.node == LoopNode.EXECUTE:
            state, stopped, grant = self._gate_or_stop(state, kind="state_graph.execute")
            grants.append(grant)
            if stopped:
                return StateGraphRunResult(
                    run_state=state,
                    resource_grants=tuple(grants),
                    evidence=collected_evidence,
                )
            if planned_step is None:
                self._discard_shadow_if_needed(spec, state.run_id, shadow_workspace)
                state = self._transition(
                    state,
                    node=LoopNode.REFLECT,
                    condition="missing_planned_capability",
                    evidence={"reason": "execute_without_planned_capability"},
                )
                state = self._transition(
                    state,
                    node=LoopNode.REFLECT,
                    condition="checker_rejected",
                    terminal_state=LoopTerminalState.FAILED,
                    evidence={"reason": "state_graph_resume_missing_plan"},
                )
                self.gateway.release()
                return StateGraphRunResult(
                    run_state=state,
                    resource_grants=tuple(grants),
                    evidence=collected_evidence,
                )
            self.store.write_checkpoint(
                state.run_id,
                node=LoopNode.EXECUTE,
                inputs={"planned_capability": planned_step.to_dict()},
                state=state.to_dict(),
            )
            executed = await self.executor_port.execute(
                planned_step,
                spec,
                state,
                workspace=execution_workspace,
            )
            collected_evidence["capability_result"] = executed.to_dict()
            if not executed.ok:
                self._discard_shadow_if_needed(spec, state.run_id, shadow_workspace)
                state = self._transition(
                    state,
                    node=LoopNode.REFLECT,
                    condition="capability_failed",
                    evidence=executed.to_dict(),
                )
                state = self._transition(
                    state,
                    node=LoopNode.REFLECT,
                    condition="checker_rejected",
                    terminal_state=LoopTerminalState.FAILED,
                    evidence={"reason": "executor_capability_failed"},
                )
                self.gateway.release()
                return StateGraphRunResult(
                    run_state=state,
                    resource_grants=tuple(grants),
                    evidence=collected_evidence,
                )
            state = self._transition(
                state,
                node=LoopNode.EVALUATE,
                condition="side_effect_recorded",
                evidence={"executor": collected_evidence["capability_result"]},
            )
            self.gateway.release()

        if state.node == LoopNode.EVALUATE:
            result = self._evaluate_state(
                spec,
                state,
                workspace=workspace,
                execution_workspace=execution_workspace,
                shadow_workspace=shadow_workspace,
                collected_evidence=collected_evidence,
                grants=grants,
                harness_results=harness_results,
                checker_report=checker_report,
            )
            if result.run_state.node == LoopNode.PLAN and not result.run_state.is_terminal():
                return await self.run_async(
                    spec,
                    workspace=workspace,
                    run_id=result.run_state.run_id,
                    evidence=result.evidence,
                )
            return result

        return StateGraphRunResult(
            run_state=state,
            checker_report=checker_report,
            resource_grants=tuple(grants),
            harness_results=tuple(harness_results),
            evidence=collected_evidence,
        )

    def _discard_shadow_if_needed(
        self,
        spec: LoopSpec,
        run_id: str,
        shadow_workspace: object | None,
    ) -> None:
        if shadow_workspace is not None and spec.rollback_policy.discard_shadow_on_failure:
            self.harness.discard_shadow_run(run_id)

    def _evaluate_state(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        workspace: Path,
        execution_workspace: Path,
        shadow_workspace: object | None,
        collected_evidence: dict[str, Any],
        grants: list[ResourceGrant],
        harness_results: list[HarnessResult],
        checker_report: CheckerReport | None,
    ) -> StateGraphRunResult:
        state, stopped, grant = self._gate_or_stop(state, kind="state_graph.evaluate")
        grants.append(grant)
        if stopped:
            return StateGraphRunResult(
                run_state=state,
                resource_grants=tuple(grants),
                evidence=collected_evidence,
            )
        self.gateway.release()
        for step in spec.verification_ladder:
            if not _step_runs_command(step):
                continue
            state, stopped, command_grant = self._gate_or_stop(
                state,
                kind="harness.command",
                request=ResourceRequest(kind="harness.command", units=1),
            )
            grants.append(command_grant)
            if stopped:
                return StateGraphRunResult(
                    run_state=state,
                    resource_grants=tuple(grants),
                    evidence=collected_evidence,
                )
            self.store.write_checkpoint(
                state.run_id,
                node=LoopNode.EVALUATE,
                inputs={"verification_step": step.to_dict()},
                state=state.to_dict(),
            )
            lock_result: LockAcquireResult | None = None
            lock_resource = _workspace_lock_resource(workspace)
            if spec.workspace_policy.require_locks:
                lock_result = self.harness.acquire_workspace_lock(
                    owner_run_id=state.run_id,
                    resource=lock_resource,
                    mode=LockMode.READ,
                )
                collected_evidence["workspace_lock"] = lock_result.to_dict()
                if not lock_result.acquired:
                    state = self._pause_for_lock_conflict(state, lock_result)
                    self.gateway.release()
                    return StateGraphRunResult(
                        run_state=state,
                        resource_grants=tuple(grants),
                        harness_results=tuple(harness_results),
                        evidence=collected_evidence,
                    )
            try:
                result = self.harness.run_command(
                    HarnessCommand(
                        command=tuple(shlex.split(step.command)),
                        cwd=execution_workspace,
                        timeout=step.timeout,
                    )
                )
            finally:
                if lock_result is not None and lock_result.acquired:
                    self.harness.release_workspace_locks(
                        owner_run_id=state.run_id,
                        resource=lock_resource,
                    )
                self.gateway.release()
            harness_results.append(result)
            collected_evidence[step.evidence_key or step.name] = result.to_facts()

        checker_report = self.checker.evaluate(spec, collected_evidence)
        if checker_report.accepted:
            if shadow_workspace is not None:
                merge_lock: LockAcquireResult | None = None
                lock_resource = _workspace_lock_resource(workspace)
                if spec.workspace_policy.require_locks:
                    merge_lock = self.harness.acquire_workspace_lock(
                        owner_run_id=state.run_id,
                        resource=lock_resource,
                        mode=LockMode.WRITE,
                    )
                    collected_evidence["merge_workspace_lock"] = merge_lock.to_dict()
                    if not merge_lock.acquired:
                        return StateGraphRunResult(
                            run_state=self._pause_for_lock_conflict(state, merge_lock),
                            checker_report=checker_report,
                            resource_grants=tuple(grants),
                            harness_results=tuple(harness_results),
                            evidence=collected_evidence,
                        )
                try:
                    merge_result = self.harness.merge_shadow_run(state.run_id)
                    collected_evidence["merge_result"] = merge_result.to_dict()
                finally:
                    if merge_lock is not None and merge_lock.acquired:
                        self.harness.release_workspace_locks(
                            owner_run_id=state.run_id,
                            resource=lock_resource,
                        )
                if merge_result.status == MergeStatus.CONFLICTED:
                    state = self._transition(
                        state,
                        node=LoopNode.EVALUATE,
                        condition="merge_conflict",
                        terminal_state=LoopTerminalState.CONFLICTED,
                        evidence=merge_result.to_dict(),
                    )
                    return StateGraphRunResult(
                        run_state=state,
                        checker_report=checker_report,
                        resource_grants=tuple(grants),
                        harness_results=tuple(harness_results),
                        evidence=collected_evidence,
                    )
            state = self._transition(
                state,
                node=LoopNode.EVALUATE,
                condition="checker_passed",
                terminal_state=LoopTerminalState.CONVERGED,
                evidence=checker_report.to_dict(),
            )
        elif checker_report.timed_out:
            self._discard_shadow_if_needed(spec, state.run_id, shadow_workspace)
            state = self._transition(
                state,
                node=LoopNode.EVALUATE,
                condition="hard_timeout",
                terminal_state=LoopTerminalState.TIMED_OUT,
                evidence=checker_report.to_dict(),
            )
        elif checker_report.blocked:
            state = self._reflect_state(
                spec,
                state,
                checker_report=checker_report,
                shadow_workspace=shadow_workspace,
                collected_evidence=collected_evidence,
                harness_results=harness_results,
            )
        else:
            state = self._reflect_state(
                spec,
                state,
                checker_report=checker_report,
                shadow_workspace=shadow_workspace,
                collected_evidence=collected_evidence,
                harness_results=harness_results,
            )

        return StateGraphRunResult(
            run_state=state,
            checker_report=checker_report,
            resource_grants=tuple(grants),
            harness_results=tuple(harness_results),
            evidence=collected_evidence,
        )

    def _reflect_state(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        checker_report: CheckerReport,
        shadow_workspace: object | None,
        collected_evidence: dict[str, Any],
        harness_results: list[HarnessResult],
    ) -> LoopRunState:
        self._discard_shadow_if_needed(spec, state.run_id, shadow_workspace)
        reflected = self._transition(
            state,
            node=LoopNode.REFLECT,
            condition="checker_failed",
            evidence=checker_report.to_dict(),
        )
        decision = self.reflector_port.reflect(
            spec,
            reflected,
            checker_report=checker_report,
            evidence=collected_evidence,
            harness_results=tuple(harness_results),
        )
        collected_evidence["reflection"] = decision.to_dict()
        if decision.retry:
            return self._transition(
                reflected,
                node=LoopNode.PLAN,
                condition="new_route_available",
                evidence=decision.to_dict(),
            )
        terminal = LoopTerminalState.BLOCKED if checker_report.blocked else LoopTerminalState.FAILED
        condition = "no_route_available" if checker_report.blocked else "checker_rejected"
        return self._transition(
            reflected,
            node=LoopNode.REFLECT,
            condition=condition,
            terminal_state=terminal,
            evidence=decision.to_dict(),
        )

    def _gate_or_stop(
        self,
        state: LoopRunState,
        *,
        kind: str,
        request: ResourceRequest | None = None,
    ) -> tuple[LoopRunState, bool, ResourceGrant]:
        grant = self.gateway.request(request or ResourceRequest(kind=kind, units=1))
        if grant.allowed:
            return state, False, grant
        return self._stop_for_resource_grant(state, grant), True, grant

    def _stop_for_resource_grant(self, state: LoopRunState, grant: ResourceGrant) -> LoopRunState:
        evidence = {"resource_grant": grant.to_dict()}
        if grant.decision == ResourceDecision.PAUSE:
            paused = self._transition(
                state,
                node=LoopNode.PAUSE,
                condition="resource_pause",
                evidence=evidence,
            )
            return self._transition(
                paused,
                node=LoopNode.PAUSE,
                condition="resource_or_user_pause",
                terminal_state=LoopTerminalState.PAUSED,
                evidence=evidence,
            )
        if grant.decision == ResourceDecision.ESCALATE:
            escalated = self._transition(
                state,
                node=LoopNode.ESCALATE,
                condition="resource_escalate",
                evidence=evidence,
            )
            return self._transition(
                escalated,
                node=LoopNode.ESCALATE,
                condition="resource_escalate",
                terminal_state=LoopTerminalState.WAITING_APPROVAL,
                evidence=evidence,
            )
        return self._transition(
            state,
            node=state.node,
            condition="resource_blocked",
            terminal_state=LoopTerminalState.BLOCKED,
            evidence=evidence,
        )

    def _pause_for_lock_conflict(
        self,
        state: LoopRunState,
        lock_result: LockAcquireResult,
    ) -> LoopRunState:
        evidence = {"workspace_lock": lock_result.to_dict(), "reason": "workspace_lock_conflict"}
        paused = self._transition(
            state,
            node=LoopNode.PAUSE,
            condition="resource_pause",
            evidence=evidence,
        )
        return self._transition(
            paused,
            node=LoopNode.PAUSE,
            condition="resource_or_user_pause",
            terminal_state=LoopTerminalState.PAUSED,
            evidence=evidence,
        )

    def _transition(
        self,
        state: LoopRunState,
        *,
        node: LoopNode | str,
        condition: str,
        terminal_state: LoopTerminalState | str = "",
        evidence: dict[str, Any] | None = None,
    ) -> LoopRunState:
        checkpoint = self._checkpoint(
            state,
            inputs={
                "target_node": str(node),
                "condition": condition,
                "terminal_state": str(terminal_state),
            },
        )
        return self.store.transition(
            state.run_id,
            node=node,
            checkpoint_id=checkpoint.id,
            terminal_state=terminal_state,
            condition=condition,
            evidence=evidence,
        )

    def _checkpoint(self, state: LoopRunState, *, inputs: dict[str, Any]) -> LoopCheckpoint:
        return self.store.write_checkpoint(
            state.run_id,
            node=state.node,
            inputs=inputs,
            state=state.to_dict(),
        )


def _step_runs_command(step: VerificationStep) -> bool:
    return bool(step.command.strip()) and step.kind in {
        VerificationKind.UNIT_TEST,
        VerificationKind.INTEGRATION_TEST,
        VerificationKind.COMMAND_EXIT_CODE,
    }


def _workspace_lock_resource(workspace: Path) -> str:
    return f"workspace:{workspace.expanduser().resolve()}"


def _checker_reason_code(checker_report: CheckerReport) -> str:
    if checker_report.timed_out:
        return "checker_timed_out"
    if checker_report.blocked:
        return "checker_blocked"
    for item in checker_report.checker_results:
        reason = str(getattr(item, "reason", "") or "").strip()
        if reason:
            return reason
    return "checker_rejected"


def _recovery_events_from_evidence(
    evidence: dict[str, Any],
    harness_results: tuple[HarnessResult, ...],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for key, value in evidence.items():
        if isinstance(value, dict):
            events.append({"tool": key, "facts": value})
    for item in harness_results:
        events.append({"tool": "harness.command", "facts": item.to_facts()})
    return events


def _goal_constraints(spec: LoopSpec) -> str:
    lines = [
        f"Objective: {spec.goal.objective}",
        "Acceptance criteria:",
        *[f"- {item}" for item in spec.goal.acceptance_criteria],
    ]
    if spec.goal.constraints:
        lines.extend(["Constraints:", *[f"- {item}" for item in spec.goal.constraints]])
    lines.append(f"Permission ceiling: {spec.goal.permission_ceiling}")
    return "\n".join(lines)
