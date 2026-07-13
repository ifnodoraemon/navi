from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from .capabilities import CapabilityRegistry
from .capabilities_types import CapabilityContext
from .lifecycle import Acceptance, Governance, Phase, Resolution
from .loop_control_service import LoopControlService, LoopControlServiceResult
from .runtime import AgentRuntime
from .state_graph import (
    CapabilityExecutorPort,
    DurableStateGraphRunner,
    LLMReflectorPort,
    LLMSemanticCheckerPort,
    ModelCapabilityPlannerPort,
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
) -> LoopControlServiceResult:
    """Execute a prepared Goal/LoopRun through the durable LLM-backed StateGraph."""
    graph_evidence = {
        "goal_id": base.goal.id,
        "run_id": base.run.id,
        **(evidence or {}),
    }
    planner_port = ModelCapabilityPlannerPort(
        runtime=runtime,
        capabilities=planner_capabilities,
        context=context,
    )
    executor_port = CapabilityExecutorPort(
        home=home,
        context=context,
        runtime=runtime,
        sensitive_approval_mode="enforce",
        governed_run_id=base.run.id,
    )
    reflector_port = LLMReflectorPort(runtime=runtime)
    checker_port = LLMSemanticCheckerPort(runtime=runtime)

    if context.trace_id:
        from .trace_proxies import (
            TracingPlannerPortProxy,
            TracingExecutorPortProxy,
            TracingReflectorPortProxy,
            TracingSemanticCheckerPortProxy,
        )
        trace_store = TraceStore(home)
        planner_port = TracingPlannerPortProxy(planner_port, trace_store, context)
        executor_port = TracingExecutorPortProxy(executor_port, trace_store, context)
        reflector_port = TracingReflectorPortProxy(reflector_port, trace_store, context)
        checker_port = TracingSemanticCheckerPortProxy(checker_port, trace_store, context)

    runner = DurableStateGraphRunner(
        home=home,
        planner_port=planner_port,
        executor_port=executor_port,
        llm_reflector_port=reflector_port,
        semantic_checker_port=checker_port,
        trace_store=TraceStore(home) if context.trace_id else None,
        trace_context=context,
    )
    graph_result = await runner.run_async(
        base.loop_spec,
        workspace=Path(base.goal.workspace),
        run_id=base.loop_run.run_id,
        evidence=graph_evidence,
    )
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
) -> LoopControlServiceResult:
    """Resume one durable loop from its persisted approval checkpoint.

    ``LoopRunStore.reopen_for_resume`` restores the EXECUTE node and the state
    graph reloads the persisted ``planned_capability`` checkpoint.  The
    capability registry then validates the approved grant against the exact
    tool, permission and canonical args before executing it.
    """
    service = LoopControlService(home)
    state = service.loop_runs.get_run(loop_run_id)
    if state is None:
        raise KeyError(f"loop run not found: {loop_run_id}")
    goal = service.goals.get(state.goal_id)
    if goal is None:
        raise KeyError(f"goal not found for loop run: {state.goal_id}")
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
            "state_transition": "approval_continuation_started",
            "loop_run_id": loop_run_id,
        },
        event_type="goal.approval_continuation_started",
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
    )
    return await run_goal_loop_state_graph(
        home=home,
        service=service,
        base=prepared,
        runtime=runtime,
        planner_capabilities=planner_capabilities,
        context=context,
        evidence={
            "entrypoint": "approval.resolve",
            "resumed": True,
            "resume_reason": "approval_approved",
        },
        result_evidence={
            "state_graph_mode": "llm_backed",
            "resumed": True,
            "resume_reason": "approval_approved",
        },
        state_transition="approval_resumed",
    )
