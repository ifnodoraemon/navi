from __future__ import annotations

import traceback
from typing import Any

from .loop import TracePhase
from .loop_contracts import LoopRunState, LoopSpec
from .trace import TraceStore
from .capabilities_types import CapabilityContext
from .state_graph import (
    ExecutedCapabilityStep,
    PlannedCapabilityStep,
    SemanticCheckDecision,
)


class TracingPlannerPortProxy:
    def __init__(self, delegate: Any, trace_store: TraceStore, trace_context: CapabilityContext):
        self.delegate = delegate
        self.trace_store = trace_store
        self.trace_context = trace_context

    @property
    def runtime(self) -> Any:
        return getattr(self.delegate, "runtime", None)

    async def plan(self, spec: LoopSpec, state: LoopRunState, *, workspace: Any, evidence: dict[str, Any]) -> PlannedCapabilityStep:
        self.trace_store.add_event(
            trace_id=self.trace_context.trace_id,
            session_id=self.trace_context.session_id or "",
            run_id=state.run_id,
            phase=str(TracePhase.PLANNER_CALL_START),
            source=self.trace_context.source,
            peer_id=self.trace_context.peer_id,
            sender_id=self.trace_context.sender_id,
            input_data={"objective": spec.goal.objective, "attempt": state.attempt},
        )

        try:
            planned_step = await self.delegate.plan(spec, state, workspace=workspace, evidence=evidence)
            
            phase = str(TracePhase.PLANNER_SYSCALL)
            if planned_step.tool == "system.planner_error":
                phase = str(TracePhase.PLANNER_CALL_ERROR)
            
            output_data = planned_step.to_dict()
            usage_data = {}
            runtime = self.runtime
            if runtime and hasattr(runtime.provider, "usage_for"):
                usage = runtime.provider.usage_for("planner")
                if usage:
                    usage_data = dict(usage)
            
            output_data["usage"] = usage_data
            prompt_messages = usage_data.pop("messages", [])
            output_data["llm_response"] = usage_data.pop("response", "")
            
            self.trace_store.add_event(
                trace_id=self.trace_context.trace_id,
                session_id=self.trace_context.session_id or "",
                run_id=state.run_id,
                phase=phase,
                source=self.trace_context.source,
                peer_id=self.trace_context.peer_id,
                sender_id=self.trace_context.sender_id,
                tool=planned_step.tool,
                model_role="planner",
                ok=(planned_step.tool != "system.planner_error"),
                input_data={"prompt": prompt_messages},
                output_data=output_data,
            )
            return planned_step
            
        except Exception as e:
            self.trace_store.add_event(
                trace_id=self.trace_context.trace_id,
                session_id=self.trace_context.session_id or "",
                run_id=state.run_id,
                phase=str(TracePhase.PLANNER_CALL_ERROR),
                source=self.trace_context.source,
                peer_id=self.trace_context.peer_id,
                sender_id=self.trace_context.sender_id,
                tool="system.planner_error",
                model_role="planner",
                ok=False,
                output_data={"error": str(e), "traceback": traceback.format_exc()},
            )
            raise


class TracingSemanticCheckerPortProxy:
    def __init__(self, delegate: Any, trace_store: TraceStore, trace_context: CapabilityContext):
        self.delegate = delegate
        self.trace_store = trace_store
        self.trace_context = trace_context

    @property
    def runtime(self) -> Any:
        return getattr(self.delegate, "runtime", None)

    async def assess(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        executed: ExecutedCapabilityStep,
        evidence: dict[str, Any],
    ) -> SemanticCheckDecision:
        self.trace_store.add_event(
            trace_id=self.trace_context.trace_id,
            session_id=self.trace_context.session_id or "",
            run_id=state.run_id,
            phase=str(TracePhase.PLANNER_CALL_START),
            source=self.trace_context.source,
            peer_id=self.trace_context.peer_id,
            sender_id=self.trace_context.sender_id,
            input_data={"objective": spec.goal.objective, "attempt": state.attempt},
        )

        try:
            decision = await self.delegate.assess(
                spec,
                state,
                executed=executed,
                evidence=evidence,
            )
            
            output_data = decision.to_dict()
            usage_data = {}
            runtime = self.runtime
            if runtime and hasattr(runtime.provider, "usage_for"):
                usage = runtime.provider.usage_for("checker")
                if usage:
                    usage_data = dict(usage)
            
            output_data["usage"] = usage_data
            prompt_messages = usage_data.pop("messages", [])
            output_data["llm_response"] = usage_data.pop("response", "")
            
            self.trace_store.add_event(
                trace_id=self.trace_context.trace_id,
                session_id=self.trace_context.session_id or "",
                run_id=state.run_id,
                phase=str(TracePhase.CAPABILITY_RESULT),
                source=self.trace_context.source,
                peer_id=self.trace_context.peer_id,
                sender_id=self.trace_context.sender_id,
                tool="checker",
                model_role="checker",
                input_data={"prompt": prompt_messages},
                output_data=output_data,
            )
            return decision
            
        except Exception as e:
            self.trace_store.add_event(
                trace_id=self.trace_context.trace_id,
                session_id=self.trace_context.session_id or "",
                run_id=state.run_id,
                phase=str(TracePhase.CAPABILITY_RESULT),
                source=self.trace_context.source,
                peer_id=self.trace_context.peer_id,
                sender_id=self.trace_context.sender_id,
                tool="checker",
                model_role="checker",
                ok=False,
                output_data={"error": str(e), "traceback": traceback.format_exc()},
            )
            raise


class TracingExecutorPortProxy:
    def __init__(self, delegate: Any, trace_store: TraceStore, trace_context: CapabilityContext):
        self.delegate = delegate
        self.trace_store = trace_store
        self.trace_context = trace_context

    @property
    def runtime(self) -> Any:
        return getattr(self.delegate, "runtime", None)

    async def execute(self, step: PlannedCapabilityStep, spec: LoopSpec, state: LoopRunState, *, workspace: Any) -> ExecutedCapabilityStep:
        try:
            executed = await self.delegate.execute(step, spec, state, workspace=workspace)
            
            self.trace_store.add_event(
                trace_id=self.trace_context.trace_id,
                session_id=self.trace_context.session_id or "",
                run_id=state.run_id,
                phase=str(TracePhase.CAPABILITY_RESULT),
                source=self.trace_context.source,
                peer_id=self.trace_context.peer_id,
                sender_id=self.trace_context.sender_id,
                tool=step.tool,
                model_role="executor",
                ok=executed.ok,
                input_data=step.to_dict(),
                output_data=executed.to_dict(),
            )
            return executed
            
        except Exception as e:
            self.trace_store.add_event(
                trace_id=self.trace_context.trace_id,
                session_id=self.trace_context.session_id or "",
                run_id=state.run_id,
                phase=str(TracePhase.CAPABILITY_RESULT),
                source=self.trace_context.source,
                peer_id=self.trace_context.peer_id,
                sender_id=self.trace_context.sender_id,
                tool=step.tool,
                model_role="executor",
                ok=False,
                input_data=step.to_dict(),
                output_data={"error": str(e), "traceback": traceback.format_exc()},
            )
            raise
