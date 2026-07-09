from __future__ import annotations

from pathlib import Path
from typing import Any

from .capabilities_types import CapabilityContext
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
    runner = DurableStateGraphRunner(
        home=home,
        planner_port=ModelCapabilityPlannerPort(
            runtime=runtime,
            capabilities=planner_capabilities,
        ),
        executor_port=CapabilityExecutorPort(home=home, context=context),
        llm_reflector_port=LLMReflectorPort(runtime=runtime),
        semantic_checker_port=LLMSemanticCheckerPort(runtime=runtime),
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
