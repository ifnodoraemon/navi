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


class _BaseTracingProxy:
    """Shared tracing infrastructure for port proxies.

    Each subclass delegates to a real port implementation and emits trace
    events around the delegated call.  The common ``add_event`` kwargs are
    centralised here so the three proxies differ only in their phase / tool /
    model_role labels.
    """

    def __init__(self, delegate: Any, trace_store: TraceStore, trace_context: CapabilityContext):
        self.delegate = delegate
        self.trace_store = trace_store
        self.trace_context = trace_context

    @property
    def runtime(self) -> Any:
        return getattr(self.delegate, "runtime", None)

    def _emit(
        self,
        *,
        state: LoopRunState,
        phase: TracePhase | str,
        model_role: str,
        tool: str = "",
        ok: bool | None = None,
        input_data: dict[str, Any] | None = None,
        output_data: dict[str, Any] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "trace_id": self.trace_context.trace_id,
            "session_id": self.trace_context.session_id or "",
            "run_id": state.run_id,
            "phase": str(phase),
            "source": self.trace_context.source,
            "peer_id": self.trace_context.peer_id,
            "sender_id": self.trace_context.sender_id,
            "tool": tool,
            "model_role": model_role,
        }
        if ok is not None:
            kwargs["ok"] = ok
        if input_data is not None:
            kwargs["input_data"] = input_data
        if output_data is not None:
            kwargs["output_data"] = output_data
        self.trace_store.add_event(**kwargs)

    def _emit_error(
        self,
        *,
        state: LoopRunState,
        phase: TracePhase | str,
        model_role: str,
        tool: str,
        error: Exception,
        input_data: dict[str, Any] | None = None,
    ) -> None:
        self._emit(
            state=state,
            phase=phase,
            model_role=model_role,
            tool=tool,
            ok=False,
            input_data=input_data,
            output_data={"error": str(error), "traceback": traceback.format_exc()},
        )

    def _extract_usage(self, role: str) -> dict[str, Any]:
        runtime = self.runtime
        if runtime and hasattr(runtime.provider, "usage_for"):
            usage = runtime.provider.usage_for(role)
            if usage:
                return dict(usage)
        return {}


class TracingPlannerPortProxy(_BaseTracingProxy):
    async def plan(
        self, spec: LoopSpec, state: LoopRunState, *, workspace: Any, evidence: dict[str, Any]
    ) -> PlannedCapabilityStep:
        self._emit(
            state=state,
            phase=TracePhase.PLANNER_CALL_START,
            model_role="planner",
            input_data={"objective": spec.goal.objective, "attempt": state.attempt},
        )
        try:
            planned_step = await self.delegate.plan(
                spec, state, workspace=workspace, evidence=evidence
            )
            usage_data = self._extract_usage("planner")
            output_data = planned_step.to_dict()
            output_data["usage"] = usage_data
            output_data["llm_response"] = usage_data.pop("response", "")
            self._emit(
                state=state,
                phase=TracePhase.PLANNER_SYSCALL,
                model_role="planner",
                tool=planned_step.tool,
                ok=True,
                input_data={"prompt": usage_data.pop("messages", [])},
                output_data=output_data,
            )
            return planned_step
        except Exception as e:
            self._emit_error(
                state=state,
                phase=TracePhase.PLANNER_CALL_ERROR,
                model_role="planner",
                tool="planner.error",
                error=e,
            )
            raise


class TracingSemanticCheckerPortProxy(_BaseTracingProxy):
    async def assess(
        self,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        executed: ExecutedCapabilityStep,
        evidence: dict[str, Any],
    ) -> SemanticCheckDecision:
        self._emit(
            state=state,
            phase=TracePhase.CAPABILITY_RESULT,
            model_role="checker",
            input_data={"objective": spec.goal.objective, "attempt": state.attempt},
        )
        try:
            decision = await self.delegate.assess(
                spec, state, executed=executed, evidence=evidence
            )
            usage_data = self._extract_usage("checker")
            output_data = decision.to_dict()
            output_data["usage"] = usage_data
            output_data["llm_response"] = usage_data.pop("response", "")
            self._emit(
                state=state,
                phase=TracePhase.CAPABILITY_RESULT,
                model_role="checker",
                tool="checker",
                input_data={"prompt": usage_data.pop("messages", [])},
                output_data=output_data,
            )
            return decision
        except Exception as e:
            self._emit_error(
                state=state,
                phase=TracePhase.CAPABILITY_RESULT,
                model_role="checker",
                tool="checker",
                error=e,
            )
            raise


class TracingExecutorPortProxy(_BaseTracingProxy):
    async def execute(
        self,
        step: PlannedCapabilityStep,
        spec: LoopSpec,
        state: LoopRunState,
        *,
        workspace: Any,
    ) -> ExecutedCapabilityStep:
        try:
            executed = await self.delegate.execute(step, spec, state, workspace=workspace)
            self._emit(
                state=state,
                phase=TracePhase.CAPABILITY_RESULT,
                model_role="executor",
                tool=step.tool,
                ok=executed.ok,
                input_data=step.to_dict(),
                output_data=executed.to_dict(),
            )
            return executed
        except Exception as e:
            self._emit_error(
                state=state,
                phase=TracePhase.CAPABILITY_RESULT,
                model_role="executor",
                tool=step.tool,
                error=e,
                input_data=step.to_dict(),
            )
            raise
