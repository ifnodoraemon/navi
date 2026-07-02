from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from ._engine_phases import EnginePhasesMixin
from .capabilities import CapabilityContext, CapabilityRegistry
from .context import ContextManager
from .control import CurrentStateBuilder, SurfaceContext, current_state_facts
from .engine_types import AgentTurnResult
from .loop import (
    LoopDecision,
    LoopProgressGate,
    TracePhase,
    TraceFailureDomain,
)
from .loop_control import (
    LoopControlEffect,
    LoopControlResult,
    RecoveryStepFrame,
    RuntimeStepFrame,
    failure_decision_for_return,
    reduce_recovery_step,
    reduce_runtime_step,
    semantic_progress_signature,
    terminal_loop_decision,
)
from .operating_context import PERMISSION_ORDER
from .runtime import AgentRuntime
from .runs import RunStore
from .syscalls import ModelSyscallPlanner
from .trace import TraceStore

logger = logging.getLogger("navi.engine")

_MAX_OBSERVATIONS_BEFORE_COMPACT = 6
_CONVERSATION_CONTEXT_MESSAGE_LIMIT = 100

__all__ = ["AgentTurnResult", "HernessEngine"]


class HernessEngine(EnginePhasesMixin):
    """Model-owned observe/plan/syscall/observe loop."""

    def __init__(
        self,
        *,
        home: Path,
        runtime: AgentRuntime,
        project_dir: Path,
        allow_sources: set[str] | None = None,
        allowed_tools: set[str] | None = None,
        disabled_tools: set[str] | None = None,
        disabled_capability_classes: frozenset[str] | frozenset = frozenset(),
        permission_ceiling: str = "write",
        event_bus: Any | None = None,
        execution_context: str = "turn",
        enforce_connector_source_policy: bool = True,
        governed_run_id: str | None = None,
        governed_workflow_id: str | None = None,
    ):
        self.home = home
        self.project_dir = project_dir
        self.runtime = runtime
        self.permission_ceiling = permission_ceiling
        self.event_bus = event_bus
        self.capabilities = CapabilityRegistry(
            home=home,
            project_dir=project_dir,
            allow_sources=allow_sources,
            allowed_tools=allowed_tools,
            disabled_tools=disabled_tools,
            disabled_capability_classes=disabled_capability_classes,
            permission_ceiling=permission_ceiling,
            execution_context=execution_context,
            enforce_connector_source_policy=enforce_connector_source_policy,
            governed_run_id=governed_run_id,
        )
        self.planner = ModelSyscallPlanner(runtime.provider)
        self.trace = TraceStore(home)
        self.context_manager = ContextManager()
        self.governed_run_id = governed_run_id or ""
        self.governed_workflow_id = governed_workflow_id or ""
        self._memory_sem: asyncio.Semaphore | None = None
        self._background_tasks: set[asyncio.Task] = set()

    @staticmethod
    def _completion_surface_text(
        result: AgentTurnResult,
        control: LoopControlResult,
    ) -> str:
        if control.convergence_message:
            return ""
        text = result.text.strip()
        if text and not text.startswith(("{", "[")):
            return text
        # No surface message and no usable text. The runtime must not synthesize
        # a natural-language completion on the model's behalf.
        return ""

    def _initialize_turn(
        self,
        text: str,
        peer_id: str,
        sender_id: str,
        source: str,
        session_id: str | None,
        session_alias: str | None,
        intent_facts: dict[str, Any] | None,
        trace_id: str | None = None,
    ) -> tuple[str, str, CapabilityContext, SurfaceContext, list[str]]:
        """Initialize turn: resolve session, create trace, build context and observations."""
        resolved_session_id = session_id
        if not resolved_session_id and session_alias:
            resolved_session_id = self.runtime.memory.current_session_id(session_alias)
        trace_id = trace_id or self.trace.new_trace_id()
        self.trace.add_event(
            trace_id=trace_id,
            phase=TracePhase.TURN_START,
            session_id=resolved_session_id or "",
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            input_data={"message": text, "session_alias": session_alias or ""},
        )

        context = CapabilityContext(
            home=self.home,
            peer_id=peer_id,
            sender_id=sender_id,
            source=source,
            permission_ceiling=self._get_effective_permission_ceiling(
                source=source,
                peer_id=peer_id,
                sender_id=sender_id,
            ),
            workspace=str(self.project_dir.resolve()),
            session_id=resolved_session_id,
            trace_id=trace_id,
            input_text=text,
            event_bus=self.event_bus,
        )

        observations: list[str] = []
        state_context = SurfaceContext(
            home=self.home,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            session_id=resolved_session_id,
            workspace=context.workspace,
            input_text=text,
        )
        current_state = CurrentStateBuilder(self.home).build(state_context)
        observations.append(
            _observation_event("current_state", current_state_facts(current_state))
        )
        dynamic_intent_facts = _dynamic_intent_facts(intent_facts)
        if dynamic_intent_facts:
            observations.append(
                _observation_event("dynamic_intent", dynamic_intent_facts)
            )

        return resolved_session_id, trace_id, context, state_context, observations

    def _get_effective_permission_ceiling(
        self,
        *,
        source: str,
        peer_id: str,
        sender_id: str,
    ) -> str:
        approval = RunStore(self.home).active_session_elevation(
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
        )
        if approval is None or not approval.requested_permission:
            return self.permission_ceiling
        return _max_permission(self.permission_ceiling, approval.requested_permission)

    async def handle(
        self,
        text: str,
        *,
        peer_id: str,
        sender_id: str,
        source: str,
        session_id: str | None = None,
        session_alias: str | None = None,
        intent_facts: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> AgentTurnResult:
        # Phase 1: Setup and initialization
        resolved_session_id, trace_id, context, state_context, observations = (
            self._initialize_turn(text, peer_id, sender_id, source, session_id, session_alias, intent_facts, trace_id)
        )

        # Phase 2: Main ReAct loop (observe → plan → execute → reflect)
        completion_events: list[dict[str, Any]] = []
        goal_ids: set[str] = set()

        progress_gate = LoopProgressGate()
        output_progress_gate = LoopProgressGate()
        loop_warning = ""

        try:
            while True:
                if len(observations) > _MAX_OBSERVATIONS_BEFORE_COMPACT:
                    compacted = self._compact_observations(observations)
                    observations = [compacted]

                step_result = await self._react_step(
                    text=text,
                    trace_id=trace_id,
                    resolved_session_id=resolved_session_id,
                    source=source,
                    peer_id=peer_id,
                    sender_id=sender_id,
                    context=context,
                    state_context=state_context,
                    observations=observations + [loop_warning] if loop_warning else observations,
                    completion_events=completion_events,
                )

                if step_result.should_return:
                    # Terminal result from within step (planner error, parse failure, or early return)
                    result = self._with_trace(step_result.result, trace_id)
                    self._record_loop_decision(
                        trace_id,
                        decision=failure_decision_for_return(result, tool=step_result.tool),
                        result=result,
                        resolved_session_id=resolved_session_id,
                        source=source,
                        peer_id=peer_id,
                        sender_id=sender_id,
                    )
                    self._record_trace_final(
                        result, trace_id, source=source, peer_id=peer_id, sender_id=sender_id
                    )
                    return result

                if step_result.should_continue:
                    # Recovery triggered; continue loop with updated observations
                    observations.append(step_result.recovery_observation)
                    control = reduce_recovery_step(
                        RecoveryStepFrame(
                            result=step_result.result,
                            facts=step_result.invoked_facts,
                            tool=step_result.tool,
                            progress_signature=step_result.progress_signature,
                            output_signature=step_result.output_signature,
                            goal_ids=goal_ids,
                            observations_count=len(observations),
                            recovery_observation=step_result.recovery_observation,
                        ),
                        progress_gate=progress_gate,
                        output_progress_gate=output_progress_gate,
                    )
                    self._record_loop_control(
                        trace_id,
                        control,
                        result=step_result.result,
                        resolved_session_id=resolved_session_id,
                        source=source,
                        peer_id=peer_id,
                        sender_id=sender_id,
                    )
                    if control.runtime_observation:
                        loop_warning = control.runtime_observation
                    else:
                        loop_warning = ""
                    if control.effect == LoopControlEffect.FINALIZE_STABLE:
                        result = step_result.result
                        self.trace.add_event(
                            trace_id=trace_id,
                            phase=TracePhase.RUNTIME_CONVERGED,
                            session_id=resolved_session_id or "",
                            run_id=result.run_id,
                            source=source,
                            peer_id=peer_id,
                            sender_id=sender_id,
                            model_role="runtime",
                            ok=True,
                            output_data={
                                "observations_count": len(observations),
                                "signature": control.progress_signature,
                            },
                            message=control.convergence_message,
                        )
                        # We are finalizing the loop. Directly throw the raw facts without summarizing on behalf of the LLM.
                        obs_lines = observations
                        if control.convergence_message:
                            obs_lines = observations + [
                                json.dumps(
                                    {
                                        "observation_type": "loop_progress_fact",
                                        "facts": {
                                            "reason": "loop_converged",
                                            "convergence_message": control.convergence_message,
                                        },
                                    },
                                    ensure_ascii=False,
                                    sort_keys=True,
                                )
                            ]
                        final_facts = {}
                        if control.decisions and control.decisions[0].evidence:
                            final_facts.update(control.decisions[0].evidence)
                        if control.convergence_message:
                            final_facts["convergence_message"] = control.convergence_message
                        surface_text = self._completion_surface_text(result, control)
                        
                        final_result = AgentTurnResult(
                            text=surface_text,
                            action="execute:system.loop_converged" if control.convergence_message else "execute:system.task_complete",
                            observation="\n\n".join(obs_lines),
                            model_role="planner",
                            terminal=True,
                            ok=not bool(control.convergence_message),
                            error_reason="loop_converged" if control.convergence_message else "",
                            trace_id=trace_id,
                            facts=final_facts
                        )
                        self._record_trace_final(final_result, trace_id, source=source, peer_id=peer_id, sender_id=sender_id)
                        turn_res = self._record_turn(text, final_result, session_id=resolved_session_id)
                        self._attach_goals(
                            goal_ids,
                            trace_id=trace_id,
                            session_id=turn_res.session_id,
                            evidence={"final_action": turn_res.action},
                        )
                        self._trigger_background_memory(turn_res)
                        return turn_res
                    continue

                # Update loop state from successful step
                result = step_result.result

                goal_id = str((step_result.invoked_facts or {}).get("goal_id") or "").strip()
                if goal_id:
                    goal_ids.add(goal_id)

                if result.terminal:
                    # Terminal condition met; finalize and return
                    self._record_loop_decision(
                        trace_id,
                        decision=terminal_loop_decision(
                            result,
                            step_result.invoked_facts,
                            tool=step_result.tool,
                            goal_ids=goal_ids,
                        ),
                        result=result,
                        resolved_session_id=resolved_session_id,
                        source=source,
                        peer_id=peer_id,
                        sender_id=sender_id,
                    )
                    turn_res = self._record_turn(text, result, session_id=resolved_session_id)
                    turn_res = self._with_trace(turn_res, trace_id)
                    self._attach_goals(
                        goal_ids,
                        trace_id=trace_id,
                        session_id=turn_res.session_id,
                        evidence={"final_action": turn_res.action},
                    )
                    self._record_trace_final(
                        turn_res, trace_id, source=source, peer_id=peer_id, sender_id=sender_id
                    )
                    self._trigger_background_memory(turn_res)
                    return turn_res

                observation = result.observation or result.text
                if observation and not loop_warning:
                    observations.append(observation)

                control = reduce_runtime_step(
                    RuntimeStepFrame(
                        result=result,
                        facts=step_result.invoked_facts,
                        tool=step_result.tool,
                        progress_signature=step_result.progress_signature,
                        output_signature=step_result.output_signature,
                        goal_ids=goal_ids,
                        observations_count=len(observations),
                    ),
                    progress_gate=progress_gate,
                    output_progress_gate=output_progress_gate,
                )
                self._record_loop_control(
                    trace_id,
                    control,
                    result=result,
                    resolved_session_id=resolved_session_id,
                    source=source,
                    peer_id=peer_id,
                    sender_id=sender_id,
                )
                if control.runtime_observation:
                    loop_warning = control.runtime_observation
                else:
                    loop_warning = ""
                if control.effect == LoopControlEffect.FINALIZE_STABLE:
                    if control.convergence_message:
                        self.trace.add_event(
                            trace_id=trace_id,
                            phase=TracePhase.RUNTIME_CONVERGED,
                            session_id=resolved_session_id or "",
                            run_id=result.run_id,
                            source=source,
                            peer_id=peer_id,
                            sender_id=sender_id,
                            model_role="runtime",
                            ok=True,
                            output_data={
                                "observations_count": len(observations),
                                "signature": control.progress_signature,
                            },
                            message=control.convergence_message,
                        )
                    obs_lines = observations
                    if control.convergence_message:
                        obs_lines = observations + [
                            json.dumps(
                                {
                                    "observation_type": "loop_progress_fact",
                                    "facts": {
                                        "reason": "loop_converged",
                                        "convergence_message": control.convergence_message,
                                    },
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                        ]
                    final_facts = {}
                    if control.decisions and control.decisions[0].evidence:
                        final_facts.update(control.decisions[0].evidence)
                    if control.convergence_message:
                        final_facts["convergence_message"] = control.convergence_message
                    surface_text = self._completion_surface_text(result, control)
                    
                    final_result = AgentTurnResult(
                        text=surface_text,
                        action="execute:system.loop_converged" if control.convergence_message else "execute:system.task_complete",
                        observation="\n\n".join(obs_lines),
                        model_role="planner",
                        terminal=True,
                        ok=not bool(control.convergence_message),
                        error_reason="loop_converged" if control.convergence_message else "",
                        trace_id=trace_id,
                        facts=final_facts
                    )
                    self._record_trace_final(final_result, trace_id, source=source, peer_id=peer_id, sender_id=sender_id)
                    turn_res = self._record_turn(text, final_result, session_id=resolved_session_id)
                    self._attach_goals(
                        goal_ids,
                        trace_id=trace_id,
                        session_id=turn_res.session_id,
                        evidence={"final_action": turn_res.action},
                    )
                    self._trigger_background_memory(turn_res)
                    return turn_res
        except Exception as e:
            import logging
            logger = logging.getLogger("navi.engine")
            logger.error(f"Engine crashed during turn {trace_id}", exc_info=True)
            
            crash_result = AgentTurnResult(
                text="",
                action="execute:system.engine_crash",
                observation=f"Engine crash: {e}",
                model_role="system",
                terminal=True,
                ok=False,
                error_reason="engine_crash",
                trace_id=trace_id,
                facts={"error_type": type(e).__name__, "error_message": str(e)},
            )
            self._record_trace_final(crash_result, trace_id, source=source, peer_id=peer_id, sender_id=sender_id)
            return crash_result


    def _record_loop_decision(
        self,
        trace_id: str,
        *,
        decision: LoopDecision,
        result: AgentTurnResult | None,
        resolved_session_id: str | None,
        source: str,
        peer_id: str,
        sender_id: str,
    ) -> None:
        run_id = decision.run_id or (result.run_id if result is not None else "")
        if run_id != decision.run_id:
            decision = replace(decision, run_id=run_id)
        self.trace.add_loop_decision(
            trace_id=trace_id,
            decision=decision,
            session_id=resolved_session_id or "",
            run_id=run_id,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
        )

    def _record_loop_control(
        self,
        trace_id: str,
        control,
        *,
        result: AgentTurnResult | None,
        resolved_session_id: str | None,
        source: str,
        peer_id: str,
        sender_id: str,
    ) -> None:
        for decision in control.decisions:
            self._record_loop_decision(
                trace_id,
                decision=decision,
                result=result,
                resolved_session_id=resolved_session_id,
                source=source,
                peer_id=peer_id,
                sender_id=sender_id,
            )

    @dataclass(frozen=True)
    class _StepResult:
        """Result from a single ReAct step."""
        result: AgentTurnResult  # The turn result from this step
        invoked_facts: dict[str, Any] | None = None  # Facts from capability invocation
        should_return: bool = False  # True if must return immediately (error or early exit)
        should_continue: bool = False  # True if recovery triggered, continue loop
        recovery_observation: str = ""  # Observation to append if continuing
        progress_signature: str = ""
        output_signature: str = ""
        tool: str = ""

    async def _react_step(
        self,
        *,
        text: str,
        trace_id: str,
        resolved_session_id: str | None,
        source: str,
        peer_id: str,
        sender_id: str,
        context: CapabilityContext,
        state_context: SurfaceContext,
        observations: list[str],
        completion_events: list[dict[str, Any]],
    ) -> _StepResult:
        """Execute one ReAct step: plan syscall, invoke capability, verify completion."""
        # Principle 12: reload durable constraints at the start of every step
        durable_constraints = self.runtime.memory.render_durable_constraints()
        planner_specs = self.capabilities.planner_specs(
            permission_ceiling=context.permission_ceiling,
            context=context,
        )
        valid_tools = {spec.name for spec in planner_specs}

        self.trace.add_event(
            trace_id=trace_id,
            phase=TracePhase.PLANNER_CALL_START,
            session_id=resolved_session_id or "",
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            model_role="planner",
            input_data={
                "observations_count": len(observations),
                "permission_ceiling": context.permission_ceiling,
                "tool_count": len(planner_specs),
            },
            message="planner provider call started",
        )

        try:
            syscall = await self.planner.plan(
                text,
                tools=planner_specs,
                conversation_context=self._conversation_context(resolved_session_id),
                observations=observations,
                permission_ceiling=context.permission_ceiling,
                model_roles=self.runtime.model_roles(),
                durable_constraints=durable_constraints,
            )
        except Exception as exc:
            self.trace.add_event(
                trace_id=trace_id,
                phase=TracePhase.PLANNER_CALL_ERROR,
                session_id=resolved_session_id or "",
                source=source,
                peer_id=peer_id,
                sender_id=sender_id,
                model_role="planner",
                ok=False,
                output_data={"error": repr(exc)},
                message=repr(exc),
            )
            result = AgentTurnResult(
                text="",
                action="execute:system.provider_error",
                model_role="planner",
                terminal=True,
                ok=False,
                error_reason="provider_no_response",
                trace_id=trace_id,
                facts={
                    "failure_domain": str(TraceFailureDomain.PROVIDER_NO_RESPONSE),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            return self._StepResult(result=result, should_return=True, tool="planner")

        output_signature = semantic_progress_signature(
            syscall.tool,
            syscall.args,
            ok=False,
            facts=None,
        )

        planner_ok = syscall.tool != "system.planner_error" and syscall.tool in valid_tools
        planner_message = syscall.reason
        if syscall.tool not in {"", "system.planner_error"} and syscall.tool not in valid_tools:
            planner_message = f"planner selected unavailable capability: {syscall.tool}"

        # Principle 14: log parse failures distinctly from successful syscalls
        is_parse_failure = syscall.tool == "system.planner_error"
        self.trace.add_event(
            trace_id=trace_id,
            phase=TracePhase.PLANNER_PARSE_ERROR if is_parse_failure else TracePhase.PLANNER_SYSCALL,
            session_id=resolved_session_id or "",
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            tool=syscall.tool,
            model_role="planner",
            ok=planner_ok,
            input_data={
                "observations_count": len(observations),
                "permission_ceiling": context.permission_ceiling,
            },
            output_data=asdict(syscall),
            message=planner_message,
        )

        if not planner_ok:
            if syscall.tool == "system.planner_error":
                error_facts = {
                    "failure_domain": str(TraceFailureDomain.PLANNER_OR_PARSER),
                    "reason": syscall.reason,
                    **syscall.args,
                }
                result = AgentTurnResult(
                    text="",
                    run_id="",
                    action="execute:system.planner_error",
                    observation=_observation_event("planner_error", error_facts),
                    model_role="planner",
                    terminal=False,
                    ok=False,
                    error_reason="planner_parse_failure",
                    trace_id=trace_id,
                    facts=error_facts,
                )
                return self._StepResult(
                    result=result,
                    should_return=False,
                    progress_signature=semantic_progress_signature(
                        syscall.tool,
                        syscall.args,
                        ok=False,
                        facts=result.facts,
                    ),
                    output_signature=output_signature,
                    tool=syscall.tool,
                )
            result = AgentTurnResult(
                text="",
                action=f"execute:{syscall.tool}",
                observation=_observation_event(
                    "planner_error",
                    {
                        "failure_domain": str(TraceFailureDomain.PLANNER_OR_PARSER),
                        "unavailable_tool": syscall.tool,
                        "reason": planner_message,
                    },
                ),
                model_role="planner",
                terminal=False,
                ok=False,
                error_reason="planner_unavailable_tool",
                facts={
                    "failure_domain": str(TraceFailureDomain.PLANNER_OR_PARSER),
                    "unavailable_tool": syscall.tool,
                },
            )
            return self._StepResult(
                result=result,
                should_return=False,
                progress_signature=semantic_progress_signature(
                    syscall.tool,
                    syscall.args,
                    ok=False,
                    facts=result.facts,
                ),
                output_signature=output_signature,
                tool=syscall.tool,
            )

        # Invoke capability
        invoked = await self.capabilities.invoke(
            syscall.tool,
            syscall.args,
            permission=syscall.permission,
            context=context,
        )

        completion_events.append(
            {
                "tool": syscall.tool,
                "ok": invoked.ok,
                "facts": invoked.facts or {},
                "action": invoked.action,
            }
        )

        self.trace.add_event(
            trace_id=trace_id,
            phase=TracePhase.CAPABILITY_RESULT,
            session_id=resolved_session_id or "",
            run_id=invoked.run_id,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            tool=syscall.tool,
            model_role=syscall.model_role,
            ok=invoked.ok,
            input_data={"args": syscall.args, "permission": syscall.permission},
            output_data={
                "action": invoked.action,
                "facts": invoked.facts or {},
                "terminal": invoked.terminal,
            },
            message=invoked.message or invoked.observation,
        )

        obs = _observation_event(
            "capability_result",
            {
                "tool": syscall.tool,
                "ok": invoked.ok,
                "action": invoked.action,
                "facts": invoked.facts or {},
                "error_reason": getattr(invoked, "error_reason", ""),
                "message": invoked.message,
            },
        )

        # Only the 'respond' capability is allowed to speak directly to the user.
        # Other tools may return 'message' for internal logging or UI hints,
        # but they must not spoof the agent's conversational text.
        text = invoked.message if syscall.tool in ("respond", "message_user") else ""
        
        result = AgentTurnResult(
            text=text,
            run_id=invoked.run_id,
            action=invoked.action,
            observation=obs,
            model_role=syscall.model_role,
            terminal=invoked.terminal,
            facts=invoked.facts,
            ok=invoked.ok,
            yields_control=getattr(invoked, "yields_control", False),
            error_reason=getattr(invoked, "error_reason", ""),
        )

        if result.terminal and not result.yields_control and observations:
            # Surface accumulated observations via observation field
            result = replace(
                result,
                observation="\n\n".join(observations),
            )

        progress_signature = semantic_progress_signature(
            syscall.tool,
            syscall.args,
            ok=invoked.ok,
            facts=invoked.facts,
        )
        return self._StepResult(
            result=result,
            invoked_facts=invoked.facts,
            progress_signature=progress_signature,
            output_signature=output_signature,
            tool=syscall.tool,
        )

    async def shutdown(self, *, timeout: float = 10.0) -> None:
        if self.event_bus:
            try:
                await asyncio.wait_for(self.event_bus.drain(), timeout=5.0)
                await asyncio.wait_for(self.event_bus.shutdown(), timeout=5.0)
            except Exception as e:
                logger.error(f"Failed to drain/shutdown event bus during engine shutdown: {e}", exc_info=True)
            try:
                await self.event_bus.shutdown()
            except Exception as e:
                logger.error(f"Failed to shut down event bus during engine shutdown: {e}", exc_info=True)

        if not self._background_tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tuple(self._background_tasks), return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            for task in list(self._background_tasks):
                task.cancel()
            await asyncio.gather(*tuple(self._background_tasks), return_exceptions=True)

    def _compact_observations(self, observations: list[str]) -> str:
        """Compact a long list of observations to prevent context window overflow."""
        if len(observations) <= 6:
            return "\n\n".join(observations)

        omitted = len(observations) - 5
        compacted = (
            observations[:2]
            + [f"... ({omitted} intermediate observations compacted) ..."]
            + observations[-3:]
        )
        return "\n\n".join(compacted)

    def _memory_semaphore(self) -> asyncio.Semaphore:
        if self._memory_sem is None:
            self._memory_sem = asyncio.Semaphore(2)
        return self._memory_sem

    def _conversation_context(self, session_id: str | None) -> str:
        if not session_id:
            return ""
        messages = self.runtime.memory.get_messages(session_id, limit=_CONVERSATION_CONTEXT_MESSAGE_LIMIT)
        return self.context_manager.build_conversation_context(messages)


def _dynamic_intent_facts(intent_facts: dict[str, Any] | None) -> dict[str, Any]:
    if not intent_facts:
        return {}
    facts = dict(intent_facts)
    if (
        facts.get("source_agent") == "intent_agent"
        and facts.get("intent_basis") == "current_state_facts"
    ):
        facts.pop("current_state", None)
        if set(facts) <= {"source_agent", "intent_basis"}:
            return {}
    return facts


def _max_permission(current: str, requested: str) -> str:
    current_level = PERMISSION_ORDER.get(current, 0)
    requested_level = PERMISSION_ORDER.get(requested, 0)
    return requested if requested_level > current_level else current


def _observation_event(kind: str, facts: dict[str, Any]) -> str:
    return json.dumps(
        {
            "observation_type": kind,
            "facts": facts,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


# Deferred import: execution -> capabilities -> connector_runtime -> engine forms a
# cycle, so we register after HernessEngine is defined to break it.
from .execution import register_engine_class  # noqa: E402

register_engine_class(HernessEngine)
