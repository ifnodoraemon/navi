from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any

from .capabilities import CapabilityRegistry
from .capabilities_types import CapabilityContext
from .lifecycle import Acceptance, Governance, Phase, Resolution
from .loop_control_service import LoopControlService, LoopControlServiceResult
from .runtime import AgentRuntime
from .resource_gateway import (
    GlobalResourceGateway,
    ResourceLimits,
    SQLiteResourceLedger,
)
from .state_graph import (
    CapabilityExecutorPort,
    DurableStateGraphRunner,
    ExecutorPort,
    LLMSemanticCheckerPort,
    ModelCapabilityPlannerPort,
    PlannerPort,
    SemanticCheckerPort,
)
from .trace import TraceStore


async def run_goal_loop_state_graph(
    *,
    home: Path,
    service: LoopControlService,
    base: LoopControlServiceResult,
    runtime: AgentRuntime,
    planner_capabilities: Any,
    context: CapabilityContext,
    evidence: dict[str, Any] | None = None,
    result_evidence: dict[str, Any] | None = None,
    state_transition: str = "opened",
    execution_owner: str = "",
) -> LoopControlServiceResult:
    """Execute a prepared Goal/LoopRun through the durable LLM-backed StateGraph."""
    trace_id = context.trace_id or base.goal.trace_id or base.run.id
    execution_context = replace(
        context,
        goal_id=base.goal.id,
        loop_run_id=base.loop_run.run_id,
        trace_id=trace_id,
    )
    graph_evidence = {
        "goal_id": base.goal.id,
        "run_id": base.run.id,
        **(evidence or {}),
    }
    planner_port: PlannerPort = ModelCapabilityPlannerPort(
        runtime=runtime,
        capabilities=planner_capabilities,
        context=execution_context,
    )
    executor_port: ExecutorPort = CapabilityExecutorPort(
        home=home,
        context=execution_context,
        runtime=runtime,
        sensitive_approval_mode="enforce",
        governed_run_id=base.run.id,
    )
    checker_port: SemanticCheckerPort = LLMSemanticCheckerPort(runtime=runtime)

    if execution_context.trace_id:
        from .trace_proxies import (
            TracingPlannerPortProxy,
            TracingExecutorPortProxy,
            TracingSemanticCheckerPortProxy,
        )
        trace_store = TraceStore(home)
        planner_port = TracingPlannerPortProxy(planner_port, trace_store, execution_context)
        executor_port = TracingExecutorPortProxy(executor_port, trace_store, execution_context)
        checker_port = TracingSemanticCheckerPortProxy(
            checker_port,
            trace_store,
            execution_context,
        )

    budget = base.loop_spec.budget_policy
    resource_gateway = GlobalResourceGateway(
        ResourceLimits(
            token_budget=budget.token_budget,
            call_budget=budget.call_budget,
            cost_budget=budget.cost_budget,
            qps_limit=budget.qps_limit,
            max_concurrent=budget.max_concurrent,
        ),
        ledger=SQLiteResourceLedger(home),
        scope_id=base.loop_run.run_id,
    )
    bind_gateway = getattr(runtime.provider, "bind_resource_gateway", None)
    runner = DurableStateGraphRunner(
        home=home,
        gateway=resource_gateway,
        planner_port=planner_port,
        executor_port=executor_port,
        semantic_checker_port=checker_port,
        trace_store=TraceStore(home) if execution_context.trace_id else None,
        trace_context=execution_context,
        execution_owner=execution_owner,
        account_phase_gates=not callable(bind_gateway),
    )
    try:
        gateway_context = (
            bind_gateway(resource_gateway) if callable(bind_gateway) else nullcontext()
        )
        with gateway_context:
            graph_result = await runner.run_async(
                base.loop_spec,
                workspace=Path(base.goal.workspace),
                run_id=base.loop_run.run_id,
                evidence=graph_evidence,
            )
    except Exception as exc:
        service.fail_state_graph_execution(
            base,
            error=exc,
            execution_owner=runner.execution_owner,
        )
        raise
    return service.apply_state_graph_result(
        base,
        graph_result,
        state_transition=state_transition,
        evidence=result_evidence,
    )


async def run_open_goal_state_graph(
    *,
    home: Path,
    service: LoopControlService,
    opened: LoopControlServiceResult,
    runtime: AgentRuntime,
    planner_capabilities: Any,
    context: CapabilityContext,
    evidence: dict[str, Any] | None = None,
    result_evidence: dict[str, Any] | None = None,
    state_transition: str = "opened",
) -> LoopControlServiceResult:
    return await run_goal_loop_state_graph(
        home=home,
        service=service,
        base=opened,
        runtime=runtime,
        planner_capabilities=planner_capabilities,
        context=context,
        evidence=evidence,
        result_evidence=result_evidence,
        state_transition=state_transition,
    )


async def resume_goal_loop_run(
    *,
    home: Path,
    loop_run_id: str,
    runtime: AgentRuntime,
    trace_id: str = "",
    input_text: str = "",
    event_bus: Any | None = None,
    entrypoint: str = "approval.resolve",
    resume_reason: str = "approval_approved",
    state_transition: str = "approval_resumed",
    resource_retry: bool = False,
    execution_owner: str = "",
) -> LoopControlServiceResult:
    """Resume one durable loop from its persisted checkpoint.

    ``LoopRunStore.reopen_for_resume`` restores the EXECUTE node and the state
    graph reloads any persisted ``planned_capability`` checkpoint. The caller
    declares why it owns this resume edge so background work is not mislabeled
    as an approval continuation.
    """
    service = LoopControlService(home)
    state = service.loop_runs.get_run(loop_run_id)
    if state is None:
        raise KeyError(f"loop run not found: {loop_run_id}")
    goal = service.goals.get(state.goal_id)
    if goal is None:
        raise KeyError(f"goal not found for loop run: {state.goal_id}")
    prior_evidence = dict(state.evidence) if resource_retry else {}
    if resource_retry and "capability_result" not in prior_evidence:
        executor = prior_evidence.get("executor")
        if isinstance(executor, dict):
            prior_evidence["capability_result"] = dict(executor)
    if resource_retry:
        service.loop_runs.reopen_resource_pause(loop_run_id)
    prepared = service.resume_loop(loop_run_id=loop_run_id, workspace=goal.workspace)
    permission_ceiling = prepared.loop_spec.goal.permission_ceiling

    running = service.runs.update_run(
        prepared.run.id,
        phase=Phase.RUNNING,
        governance=Governance.APPROVED,
        acceptance=Acceptance.NONE,
        resolution=Resolution.NONE,
        result_summary="",
        error="",
    )
    active_goal = service.goals.update_state(
        prepared.goal.id,
        phase=Phase.RUNNING,
        governance=Governance.APPROVED,
        acceptance=Acceptance.NONE,
        resolution=Resolution.NONE,
        task_status="in_progress",
        blocked_reason="",
        evidence={
            "state_transition": "loop_resume_started",
            "loop_run_id": loop_run_id,
            "entrypoint": entrypoint,
            "resume_reason": resume_reason,
        },
        event_type="goal.loop_resume_started",
    )
    if running is not None or active_goal is not None:
        prepared = replace(
            prepared,
            run=running or prepared.run,
            goal=active_goal or prepared.goal,
        )

    planner_capabilities = CapabilityRegistry(
        home=home,
        project_dir=Path(goal.workspace),
        allowed_tools=(
            None
            if "*" in prepared.loop_spec.allowed_capabilities
            else set(prepared.loop_spec.allowed_capabilities)
        ),
        permission_ceiling=permission_ceiling,
        runtime=runtime,
    )
    context = CapabilityContext(
        home=home,
        source=goal.source,
        peer_id=goal.peer_id,
        sender_id=goal.sender_id,
        session_id=goal.session_id,
        permission_ceiling=permission_ceiling,
        workspace=goal.workspace,
        trace_id=trace_id,
        input_text=input_text,
        event_bus=event_bus,
        allowed_tools=(
            None
            if "*" in prepared.loop_spec.allowed_capabilities
            else frozenset(prepared.loop_spec.allowed_capabilities)
        ),
    )
    return await run_goal_loop_state_graph(
        home=home,
        service=service,
        base=prepared,
        runtime=runtime,
        planner_capabilities=planner_capabilities,
        context=context,
        evidence={
            **prior_evidence,
            "entrypoint": entrypoint,
            "resumed": True,
            "resume_reason": resume_reason,
        },
        result_evidence={
            "state_graph_mode": "llm_backed",
            "resumed": True,
            "resume_reason": resume_reason,
        },
        state_transition=state_transition,
        execution_owner=execution_owner,
    )
