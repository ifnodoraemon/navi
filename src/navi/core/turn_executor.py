import asyncio
import json
import logging
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Set

from .._engine_phases import EnginePhasesMixin
from ..capabilities import CapabilityContext
from ..control import SurfaceContext
from ..engine_types import AgentTurnResult
from ..loop import (
    LoopDecision,
    LoopProgressGate,
    TracePhase,
    TraceFailureDomain,
)
from ..loop_control import (
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
from ..prompt_os import (
    assemble_fact_response_system_prompt,
    assemble_fact_response_turn_input,
)
from ..provider import ChatMessage
from .context_builder import ContextBuilder, _observation_event

logger = logging.getLogger("navi.engine")

_MAX_OBSERVATIONS_BEFORE_COMPACT = 6

@dataclass(frozen=True)
class _StepResult:
    """Result from a single ReAct step."""
    result: AgentTurnResult
    invoked_facts: Dict[str, Any] | None = None
    should_return: bool = False
    should_continue: bool = False
    recovery_observation: str = ""
    progress_signature: str = ""
    output_signature: str = ""
    tool: str = ""

@dataclass(frozen=True)
class _EvalResult:
    """Unified loop step evaluation outcome."""
    outcome: str  # "continue" | "recover" | "finalize" | "converged" | "failed"
    control: "LoopControlResult | None" = None
    decision: "LoopDecision | None" = None

@dataclass(frozen=True)
class _PlanOutcome:
    """Result of the Plan phase."""
    ok: bool
    syscall: Any  # the parsed Syscall from planner
    output_signature: str = ""
    # Pre-built step result when planner failed (ok=False)
    error_step: "_StepResult | None" = None

class TurnExecutor(EnginePhasesMixin):
    def __init__(
        self,
        *,
        home: Path,
        runtime: Any,
        trace: Any,
        capabilities: Any,
        planner: Any,
        event_bus: Any | None,
        context_builder: ContextBuilder,
        governed_run_id: str | None = None,
        governed_workflow_id: str | None = None,
    ):
        self.home = home
        self.runtime = runtime
        self.trace = trace
        self.capabilities = capabilities
        self.planner = planner
        self.event_bus = event_bus
        self.context_builder = context_builder
        self.governed_run_id = governed_run_id or ""
        self.governed_workflow_id = governed_workflow_id or ""
        self.permission_ceiling = context_builder.permission_ceiling
        self._background_tasks: Set[asyncio.Task] = set()

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
        return ""

    async def _model_surface_text_from_facts(
        self,
        *,
        user_text: str,
        facts: Dict[str, Any],
        observations: List[str],
        trace_id: str,
        resolved_session_id: str | None,
        source: str,
        peer_id: str,
        sender_id: str,
    ) -> str:
        if not facts:
            return ""
        messages = [
            ChatMessage(
                role="system",
                content=assemble_fact_response_system_prompt().render(),
            ),
            ChatMessage(
                role="user",
                content=assemble_fact_response_turn_input(
                    user_text=user_text,
                    facts=facts,
                    observations=observations,
                ).render(),
            ),
        ]
        try:
            text = (await self.runtime.complete(messages, role="responder")).strip()
        except Exception as exc:
            self.trace.add_event(
                trace_id=trace_id,
                phase=TracePhase.AGENT_ROLE_RESULT,
                session_id=resolved_session_id or "",
                source=source,
                peer_id=peer_id,
                sender_id=sender_id,
                model_role="responder",
                ok=False,
                input_data={"fact_keys": sorted(facts)},
                output_data={"error_type": type(exc).__name__, "error": str(exc)},
                message="",
            )
            return ""
        self.trace.add_event(
            trace_id=trace_id,
            phase=TracePhase.AGENT_ROLE_RESULT,
            session_id=resolved_session_id or "",
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            model_role="responder",
            ok=bool(text),
            input_data={"fact_keys": sorted(facts)},
            output_data={
                "text_present": bool(text),
                "provider_usage": self.runtime.usage_for("responder"),
            },
            message=text[:1600],
        )
        return text

    def _compact_observations(self, observations: List[str]) -> str:
        if len(observations) <= 6:
            return "\n\n".join(observations)

        omitted = len(observations) - 5
        compacted = (
            observations[:2]
            + [f"... ({omitted} intermediate observations compacted) ..."]
            + observations[-3:]
        )
        return "\n\n".join(compacted)

    @staticmethod
    def _stable_finalize_metadata(
        result: AgentTurnResult,
        control: LoopControlResult,
    ) -> tuple[str, bool, str]:
        if result.ok and not control.convergence_message:
            return "execute:system.task_complete", True, ""

        reason = result.error_reason or "loop_converged"
        if control.decisions:
            failure_domain = str(control.decisions[0].failure_domain or "").strip()
            if failure_domain and failure_domain != str(TraceFailureDomain.NONE):
                reason = failure_domain
        return "execute:system.loop_converged", False, reason

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
        control: LoopControlResult,
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

    # ------------------------------------------------------------------
    # Phase ①: Observe
    # ------------------------------------------------------------------

    def _observe(self, observations: List[str]) -> List[str]:
        """Compact observations if they exceed the window limit."""
        if len(observations) > _MAX_OBSERVATIONS_BEFORE_COMPACT:
            return [self._compact_observations(observations)]
        return observations

    # ------------------------------------------------------------------
    # Phase ②: Plan
    # ------------------------------------------------------------------

    async def _plan(
        self,
        text: str,
        observations: List[str],
        *,
        context: CapabilityContext,
        state_context: SurfaceContext,
        trace_id: str,
        resolved_session_id: str | None,
        source: str,
        peer_id: str,
        sender_id: str,
    ) -> _PlanOutcome:
        """Call the Planner (LLM) and validate the returned Syscall.

        Returns a ``_PlanOutcome`` that is always safe to pass to ``_act``.
        When the planner fails (provider crash, parse error, unavailable tool),
        ``_PlanOutcome.ok`` is False and ``error_step`` carries the pre-built
        ``_StepResult`` so that ``_act`` can short-circuit.
        """
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
                conversation_context=self.context_builder.conversation_context(resolved_session_id),
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
            return _PlanOutcome(
                ok=False, syscall=None, output_signature="",
                error_step=_StepResult(result=result, should_return=True, tool="planner"),
            )

        output_signature = semantic_progress_signature(
            syscall.tool, syscall.args, ok=False, facts=None,
        )

        planner_ok = syscall.tool != "system.planner_error" and syscall.tool in valid_tools
        planner_message = syscall.reason
        if syscall.tool not in {"", "system.planner_error"} and syscall.tool not in valid_tools:
            planner_message = f"planner selected unavailable capability: {syscall.tool}"

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
            output_data={
                **asdict(syscall),
                "provider_usage": self.runtime.usage_for("planner"),
            },
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
                error_step = _StepResult(
                    result=result,
                    should_return=False,
                    progress_signature=semantic_progress_signature(
                        syscall.tool, syscall.args, ok=False, facts=result.facts,
                    ),
                    output_signature=output_signature,
                    tool=syscall.tool,
                )
            else:
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
                error_step = _StepResult(
                    result=result,
                    should_return=False,
                    progress_signature=semantic_progress_signature(
                        syscall.tool, syscall.args, ok=False, facts=result.facts,
                    ),
                    output_signature=output_signature,
                    tool=syscall.tool,
                )
            return _PlanOutcome(
                ok=False, syscall=syscall, output_signature=output_signature,
                error_step=error_step,
            )

        return _PlanOutcome(ok=True, syscall=syscall, output_signature=output_signature)

    # ------------------------------------------------------------------
    # Phase ③: Act
    # ------------------------------------------------------------------

    async def _act(
        self,
        plan: _PlanOutcome,
        *,
        context: CapabilityContext,
        observations: List[str],
        completion_events: List[Dict[str, Any]],
        trace_id: str,
        resolved_session_id: str | None,
        source: str,
        peer_id: str,
        sender_id: str,
    ) -> _StepResult:
        """Execute the planned Syscall via the CapabilityRegistry.

        If the plan failed, short-circuits with the pre-built error step.
        """
        if not plan.ok:
            assert plan.error_step is not None
            return plan.error_step

        syscall = plan.syscall
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

        text = invoked.message if syscall.tool in ("respond", "message_user", "delegate.reply") else ""

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
        return _StepResult(
            result=result,
            invoked_facts=invoked.facts,
            progress_signature=progress_signature,
            output_signature=plan.output_signature,
            tool=syscall.tool,
        )

    # Loop evaluation: unified Check + Decide
    # ------------------------------------------------------------------

    def _evaluate_step(
        self,
        step: _StepResult,
        *,
        progress_gate: LoopProgressGate,
        output_progress_gate: LoopProgressGate,
        goal_ids: Set[str],
        observations_count: int,
    ) -> _EvalResult:
        """Unified Check + Decide: produce a single outcome for every step.

        This is the sole decision point in the loop.  Every path through the
        loop (fatal, recovery, terminal, normal) goes through here, producing
        one of five outcomes: failed, recover, finalize, converged, continue.
        """
        # Fatal error (provider crash, etc.)
        if step.should_return:
            return _EvalResult(
                outcome="failed",
                decision=failure_decision_for_return(step.result, tool=step.tool),
            )

        # Recovery path (checker rejected the result)
        if step.should_continue:
            control = reduce_recovery_step(
                RecoveryStepFrame(
                    result=step.result,
                    facts=step.invoked_facts,
                    tool=step.tool,
                    progress_signature=step.progress_signature,
                    output_signature=step.output_signature,
                    goal_ids=goal_ids,
                    observations_count=observations_count + 1,
                    recovery_observation=step.recovery_observation,
                ),
                progress_gate=progress_gate,
                output_progress_gate=output_progress_gate,
            )
            outcome = "converged" if control.effect == LoopControlEffect.FINALIZE_STABLE else "recover"
            return _EvalResult(outcome=outcome, control=control)

        # Model-initiated terminal (e.g. called respond)
        if step.result.terminal:
            return _EvalResult(
                outcome="finalize",
                decision=terminal_loop_decision(
                    step.result, step.invoked_facts,
                    tool=step.tool, goal_ids=goal_ids,
                ),
            )

        # Normal non-terminal step
        control = reduce_runtime_step(
            RuntimeStepFrame(
                result=step.result,
                facts=step.invoked_facts,
                tool=step.tool,
                progress_signature=step.progress_signature,
                output_signature=step.output_signature,
                goal_ids=goal_ids,
                observations_count=observations_count,
            ),
            progress_gate=progress_gate,
            output_progress_gate=output_progress_gate,
        )
        outcome = "converged" if control.effect == LoopControlEffect.FINALIZE_STABLE else "continue"
        return _EvalResult(outcome=outcome, control=control)

    # ------------------------------------------------------------------
    # Exit paths (each written exactly once)
    # ------------------------------------------------------------------

    def _finalize_failed(
        self,
        step: _StepResult,
        decision: LoopDecision,
        *,
        trace_id: str,
        resolved_session_id: str | None,
        source: str,
        peer_id: str,
        sender_id: str,
    ) -> AgentTurnResult:
        """Exit: fatal error (provider crash, unrecoverable)."""
        result = self._with_trace(step.result, trace_id)
        self._record_loop_decision(
            trace_id, decision=decision, result=result,
            resolved_session_id=resolved_session_id,
            source=source, peer_id=peer_id, sender_id=sender_id,
        )
        self._record_trace_final(
            result, trace_id, source=source, peer_id=peer_id, sender_id=sender_id,
        )
        return result

    async def _finalize_terminal(
        self,
        step: _StepResult,
        decision: LoopDecision,
        *,
        text: str,
        observations: List[str],
        trace_id: str,
        resolved_session_id: str | None,
        source: str,
        peer_id: str,
        sender_id: str,
        goal_ids: Set[str],
    ) -> AgentTurnResult:
        """Exit: model-initiated termination (e.g. called respond)."""
        result = step.result
        if not result.text and result.facts:
            surface_text = await self._model_surface_text_from_facts(
                user_text=text, facts=result.facts,
                observations=observations + ([result.observation] if result.observation else []),
                trace_id=trace_id, resolved_session_id=resolved_session_id,
                source=source, peer_id=peer_id, sender_id=sender_id,
            )
            if surface_text:
                result = replace(result, text=surface_text, model_role="responder")
        self._record_loop_decision(
            trace_id, decision=decision, result=result,
            resolved_session_id=resolved_session_id,
            source=source, peer_id=peer_id, sender_id=sender_id,
        )
        turn_res = self._record_turn(text, result, session_id=resolved_session_id)
        turn_res = self._with_trace(turn_res, trace_id)
        self._attach_goals(
            goal_ids, trace_id=trace_id,
            session_id=turn_res.session_id,
            evidence={"final_action": turn_res.action},
        )
        self._record_trace_final(
            turn_res, trace_id, source=source, peer_id=peer_id, sender_id=sender_id,
        )
        self._trigger_background_memory(turn_res)
        return turn_res

    async def _finalize_stable(
        self,
        step: _StepResult,
        control: LoopControlResult,
        *,
        text: str,
        observations: List[str],
        trace_id: str,
        resolved_session_id: str | None,
        source: str,
        peer_id: str,
        sender_id: str,
        goal_ids: Set[str],
    ) -> AgentTurnResult:
        """Exit: convergence (ProgressGate detected repetition)."""
        result = step.result
        if control.convergence_message:
            self.trace.add_event(
                trace_id=trace_id,
                phase=TracePhase.RUNTIME_CONVERGED,
                session_id=resolved_session_id or "",
                run_id=result.run_id,
                source=source, peer_id=peer_id, sender_id=sender_id,
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

        final_facts: Dict[str, Any] = {}
        if control.decisions and control.decisions[0].evidence:
            final_facts.update(control.decisions[0].evidence)
        if control.convergence_message:
            final_facts["convergence_message"] = control.convergence_message

        action, ok, error_reason = self._stable_finalize_metadata(result, control)
        if not ok:
            final_facts.setdefault("ok", False)
            final_facts.setdefault("error_reason", error_reason)

        surface_text = self._completion_surface_text(result, control)
        if not surface_text and final_facts:
            surface_text = await self._model_surface_text_from_facts(
                user_text=text, facts=final_facts, observations=obs_lines,
                trace_id=trace_id, resolved_session_id=resolved_session_id,
                source=source, peer_id=peer_id, sender_id=sender_id,
            )

        final_result = AgentTurnResult(
            text=surface_text,
            action=action,
            observation="\n\n".join(obs_lines),
            model_role="planner",
            terminal=True,
            ok=ok,
            error_reason=error_reason,
            trace_id=trace_id,
            facts=final_facts,
        )
        self._record_trace_final(
            final_result, trace_id, source=source, peer_id=peer_id, sender_id=sender_id,
        )
        turn_res = self._record_turn(text, final_result, session_id=resolved_session_id)
        self._attach_goals(
            goal_ids, trace_id=trace_id,
            session_id=turn_res.session_id,
            evidence={"final_action": turn_res.action},
        )
        self._trigger_background_memory(turn_res)
        return turn_res

    # ------------------------------------------------------------------
    # Main loop: Observe → Plan → Act → Check → Decide → Dispatch
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Main loop: Observe → Plan → Act → Check → Decide → Dispatch
    # ------------------------------------------------------------------

    async def handle_loop(
        self,
        text: str,
        *,
        peer_id: str,
        sender_id: str,
        source: str,
        session_id: str | None = None,
        session_alias: str | None = None,
        intent_facts: Dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> AgentTurnResult:
        resolved_session_id, trace_id, context, state_context, observations = (
            self.context_builder.initialize_turn(
                text, peer_id, sender_id, source, session_id, session_alias, intent_facts, trace_id
            )
        )

        completion_events: List[Dict[str, Any]] = []
        goal_ids: Set[str] = set()
        progress_gate = LoopProgressGate()
        output_progress_gate = LoopProgressGate()
        loop_warning = ""

        try:
            while True:
                # ① Observe
                observations = self._observe(observations)

                # ② Plan
                plan = await self._plan(
                    text,
                    observations + ([loop_warning] if loop_warning else []),
                    context=context,
                    state_context=state_context,
                    trace_id=trace_id,
                    resolved_session_id=resolved_session_id,
                    source=source, peer_id=peer_id, sender_id=sender_id,
                )

                # ③ Act
                step = await self._act(
                    plan,
                    context=context,
                    observations=observations,
                    completion_events=completion_events,
                    trace_id=trace_id,
                    resolved_session_id=resolved_session_id,
                    source=source, peer_id=peer_id, sender_id=sender_id,
                )

                # ④ Check + Decide
                evaluation = self._evaluate_step(
                    step,
                    progress_gate=progress_gate,
                    output_progress_gate=output_progress_gate,
                    goal_ids=goal_ids,
                    observations_count=len(observations),
                )

                # ⑤ Dispatch
                outcome = evaluation.outcome

                if outcome == "failed":
                    assert evaluation.decision is not None
                    return self._finalize_failed(
                        step, evaluation.decision,
                        trace_id=trace_id,
                        resolved_session_id=resolved_session_id,
                        source=source, peer_id=peer_id, sender_id=sender_id,
                    )

                if outcome == "finalize":
                    assert evaluation.decision is not None
                    return await self._finalize_terminal(
                        step, evaluation.decision,
                        text=text, observations=observations,
                        trace_id=trace_id,
                        resolved_session_id=resolved_session_id,
                        source=source, peer_id=peer_id, sender_id=sender_id,
                        goal_ids=goal_ids,
                    )

                if outcome == "converged":
                    assert evaluation.control is not None
                    self._record_loop_control(
                        trace_id, evaluation.control,
                        result=step.result,
                        resolved_session_id=resolved_session_id,
                        source=source, peer_id=peer_id, sender_id=sender_id,
                    )
                    return await self._finalize_stable(
                        step, evaluation.control,
                        text=text, observations=observations,
                        trace_id=trace_id,
                        resolved_session_id=resolved_session_id,
                        source=source, peer_id=peer_id, sender_id=sender_id,
                        goal_ids=goal_ids,
                    )

                # continue / recover → back to loop top
                assert evaluation.control is not None
                self._record_loop_control(
                    trace_id, evaluation.control,
                    result=step.result,
                    resolved_session_id=resolved_session_id,
                    source=source, peer_id=peer_id, sender_id=sender_id,
                )
                loop_warning = evaluation.control.runtime_observation or ""

                if outcome == "recover":
                    observations.append(step.recovery_observation)
                else:  # "continue"
                    goal_id = str((step.invoked_facts or {}).get("goal_id") or "").strip()
                    if goal_id:
                        goal_ids.add(goal_id)
                    observation = step.result.observation or step.result.text
                    if observation and not loop_warning:
                        observations.append(observation)

        except Exception as e:
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
            self._record_trace_final(
                crash_result, trace_id, source=source, peer_id=peer_id, sender_id=sender_id,
            )
            return crash_result
