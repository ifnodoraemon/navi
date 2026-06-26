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
from .recovery import RecoveryPlanner
from .runtime import AgentRuntime
from .syscalls import ModelSyscallPlanner
from .trace import TraceStore

logger = logging.getLogger("navi.engine")

# Re-export for backward compatibility
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
    ):
        self.home = home
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
        self.recovery = RecoveryPlanner()
        self.trace = TraceStore(home)
        self.context_manager = ContextManager()
        self._memory_sem: asyncio.Semaphore | None = None
        self._background_tasks: set[asyncio.Task] = set()

    def _initialize_turn(
        self,
        text: str,
        peer_id: str,
        sender_id: str,
        source: str,
        session_id: str | None,
        session_alias: str | None,
        intent_facts: dict[str, Any] | None,
    ) -> tuple[str, str, CapabilityContext, SurfaceContext, list[str]]:
        """Initialize turn: resolve session, create trace, build context and observations."""
        resolved_session_id = session_id
        if not resolved_session_id and session_alias:
            resolved_session_id = self.runtime.memory.current_session_id(session_alias)
        trace_id = self.trace.new_trace_id()
        self.trace.add_event(
            trace_id=trace_id,
            phase="turn.start",
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
            permission_ceiling=self._get_effective_permission_ceiling(peer_id, sender_id),
            workspace=str(self.capabilities.gateway.project_dir.resolve()),
            session_id=resolved_session_id,
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
            "Current State Facts:\n"
            + json.dumps(current_state_facts(current_state), ensure_ascii=False, sort_keys=True)
        )
        if intent_facts:
            observations.append(
                "Dynamic Intent Facts:\n"
                + json.dumps(intent_facts, ensure_ascii=False, sort_keys=True)
            )

        return resolved_session_id, trace_id, context, state_context, observations

    def _get_effective_permission_ceiling(self, peer_id: str, sender_id: str) -> str:
        return self.permission_ceiling

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
    ) -> AgentTurnResult:
        # Phase 1: Setup and initialization
        resolved_session_id, trace_id, context, state_context, observations = (
            self._initialize_turn(text, peer_id, sender_id, source, session_id, session_alias, intent_facts)
        )

        # Phase 2: Main ReAct loop (observe → plan → execute → reflect)
        completion_events: list[dict[str, Any]] = []
        goal_ids: set[str] = set()
        pending_approval_prompt = ""
        seen_progress_signatures: set[str] = set()

        while True:
            step_result = await self._react_step(
                text=text,
                trace_id=trace_id,
                resolved_session_id=resolved_session_id,
                source=source,
                peer_id=peer_id,
                sender_id=sender_id,
                context=context,
                state_context=state_context,
                observations=observations,
                completion_events=completion_events,
            )

            if step_result.should_return:
                # Terminal result from within step (planner error, parse failure, or early return)
                return step_result.result

            if step_result.should_continue:
                # Recovery triggered; continue loop with updated observations
                observations.append(step_result.recovery_observation)
                progress_signature = step_result.progress_signature
                if progress_signature and progress_signature in seen_progress_signatures:
                    result = step_result.result
                    self.trace.add_event(
                        trace_id=trace_id,
                        phase="runtime.converged",
                        session_id=resolved_session_id or "",
                        run_id=result.run_id,
                        source=source,
                        peer_id=peer_id,
                        sender_id=sender_id,
                        model_role="runtime",
                        ok=True,
                        output_data={
                            "observations_count": len(observations),
                            "signature": progress_signature,
                        },
                        message="repeated recovery result; synthesizing stable observations",
                    )
                    return await self._finalize_stable_observations(
                        text=text,
                        observations=observations,
                        result=result,
                        goal_ids=goal_ids,
                        resolved_session_id=resolved_session_id,
                        trace_id=trace_id,
                        source=source,
                        peer_id=peer_id,
                        sender_id=sender_id,
                        pending_approval_prompt=pending_approval_prompt,
                    )
                if progress_signature:
                    seen_progress_signatures.add(progress_signature)
                continue

            # Update loop state from successful step
            result = step_result.result
            approval_prompt = self._approval_prompt_from_facts(step_result.invoked_facts, source=source)
            if approval_prompt:
                pending_approval_prompt = approval_prompt
            goal_id = str((step_result.invoked_facts or {}).get("goal_id") or "").strip()
            if goal_id:
                goal_ids.add(goal_id)

            if result.terminal:
                # Terminal condition met; finalize and return
                result = self._with_approval_affordance(result, pending_approval_prompt)
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
            if observation:
                observations.append(observation)

            if self._facts_complete_current_request(step_result.invoked_facts):
                return await self._finalize_stable_observations(
                    text=text,
                    observations=observations,
                    result=result,
                    goal_ids=goal_ids,
                    resolved_session_id=resolved_session_id,
                    trace_id=trace_id,
                    source=source,
                    peer_id=peer_id,
                    sender_id=sender_id,
                    pending_approval_prompt=pending_approval_prompt,
                )

            progress_signature = step_result.progress_signature
            if progress_signature and progress_signature in seen_progress_signatures:
                self.trace.add_event(
                    trace_id=trace_id,
                    phase="runtime.converged",
                    session_id=resolved_session_id or "",
                    run_id=result.run_id,
                    source=source,
                    peer_id=peer_id,
                    sender_id=sender_id,
                    model_role="runtime",
                    ok=True,
                    output_data={
                        "observations_count": len(observations),
                        "signature": progress_signature,
                    },
                    message="repeated capability result; synthesizing stable observations",
                )
                return await self._finalize_stable_observations(
                    text=text,
                    observations=observations,
                    result=result,
                    goal_ids=goal_ids,
                    resolved_session_id=resolved_session_id,
                    trace_id=trace_id,
                    source=source,
                    peer_id=peer_id,
                    sender_id=sender_id,
                    pending_approval_prompt=pending_approval_prompt,
                )
            if progress_signature:
                seen_progress_signatures.add(progress_signature)

    @staticmethod
    def _facts_complete_current_request(facts: dict[str, Any] | None) -> bool:
        if not isinstance(facts, dict):
            return False
        return facts.get("completion_evidence") is True

    @staticmethod
    def _progress_signature(
        tool: str,
        args: dict[str, Any],
        *,
        ok: bool,
        facts: dict[str, Any] | None,
    ) -> str:
        return json.dumps(
            {
                "tool": tool,
                "args": args,
                "ok": ok,
                "facts": facts or {},
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    async def _finalize_stable_observations(
        self,
        *,
        text: str,
        observations: list[str],
        result: AgentTurnResult,
        goal_ids: set[str],
        resolved_session_id: str | None,
        trace_id: str,
        source: str,
        peer_id: str,
        sender_id: str,
        pending_approval_prompt: str,
    ) -> AgentTurnResult:
        turn_res = await self._finalize_observations(
            text,
            observations,
            session_id=resolved_session_id,
            trace_id=trace_id,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            action=result.action,
            run_id=result.run_id,
            model_role=result.model_role,
            pending_approval_prompt=pending_approval_prompt,
        )
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

    @dataclass(frozen=True)
    class _StepResult:
        """Result from a single ReAct step."""
        result: AgentTurnResult  # The turn result from this step
        invoked_facts: dict[str, Any] | None = None  # Facts from capability invocation
        should_return: bool = False  # True if must return immediately (error or early exit)
        should_continue: bool = False  # True if recovery triggered, continue loop
        recovery_observation: str = ""  # Observation to append if continuing
        progress_signature: str = ""

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
            permission_ceiling=context.permission_ceiling
        )
        valid_tools = {spec.name for spec in planner_specs}

        self.trace.add_event(
            trace_id=trace_id,
            phase="planner.call.start",
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
                phase="planner.call.error",
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
                text=f"Planner provider failed: {exc!r}",
                action="chat",
                model_role="planner",
                terminal=True,
                trace_id=trace_id,
            )
            self._record_trace_final(
                result, trace_id, source=source, peer_id=peer_id, sender_id=sender_id
            )
            return self._StepResult(result=result, should_return=True)

        planner_ok = syscall.tool != "system.planner_error" and syscall.tool in valid_tools
        planner_message = syscall.reason
        if syscall.tool not in {"", "system.planner_error"} and syscall.tool not in valid_tools:
            planner_message = f"planner selected unavailable capability: {syscall.tool}"

        # Principle 14: log parse failures distinctly from successful syscalls
        is_parse_failure = syscall.tool == "system.planner_error"
        self.trace.add_event(
            trace_id=trace_id,
            phase="planner.parse_error" if is_parse_failure else "planner.syscall",
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
                result = AgentTurnResult(
                    text=f"Internal Error: Failed to parse planner output - {syscall.reason}",
                    run_id="",
                    action="chat",
                    observation="",
                    model_role="planner",
                    terminal=True,
                )
                return self._StepResult(result=result, should_return=True)
            result = AgentTurnResult(
                text=planner_message,
                action="capability_error",
                model_role="planner",
                terminal=True,
            )
            return self._StepResult(result=result)

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
            phase="capability.result",
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

        obs = invoked.observation
        if invoked.ok:
            if obs and not obs.startswith("["):
                obs = f"[{syscall.tool} result] {obs}"
        else:
            if obs and not obs.startswith("["):
                obs = f"[{syscall.tool} error] {obs}"

        result = AgentTurnResult(
            text=invoked.message or invoked.observation,
            run_id=invoked.run_id,
            action=invoked.action,
            observation=obs,
            model_role=syscall.model_role,
            terminal=invoked.terminal,
            facts=invoked.facts,
        )

        if result.terminal and observations and result.action == "chat":
            # Surface accumulated observations via observation field
            result = replace(
                result,
                observation="\n\n".join(observations),
            )

        if result.terminal and result.action not in ("ask.user", "ask"):
            block = self._completion_block_reason(
                completion_events,
                state_context=state_context,
            )
            if block:
                self.trace.add_event(
                    trace_id=trace_id,
                    phase="completion.verify",
                    session_id=resolved_session_id or "",
                    run_id=result.run_id,
                    source=source,
                    peer_id=peer_id,
                    sender_id=sender_id,
                    model_role="runtime",
                    ok=False,
                    input_data={"events_count": len(completion_events)},
                    output_data={"reason": block.reason},
                    message=block.reason,
                )
                recovery_plan = self.recovery.plan_completion_failure(
                    block=block,
                    events=completion_events,
                )
                self.trace.add_event(
                    trace_id=trace_id,
                    phase="recovery.plan",
                    session_id=resolved_session_id or "",
                    run_id=result.run_id,
                    source=source,
                    peer_id=peer_id,
                    sender_id=sender_id,
                    model_role="runtime",
                    ok=True,
                    input_data={"trigger": recovery_plan.trigger},
                    output_data=asdict(recovery_plan),
                    message=recovery_plan.reason,
                )
                progress_signature = self._progress_signature(
                    syscall.tool,
                    syscall.args,
                    ok=invoked.ok,
                    facts=invoked.facts,
                )
                return self._StepResult(
                    result=result,
                    invoked_facts=invoked.facts,
                    should_continue=True,
                    recovery_observation=recovery_plan.to_observation(),
                    progress_signature=progress_signature,
                )

        progress_signature = self._progress_signature(
            syscall.tool,
            syscall.args,
            ok=invoked.ok,
            facts=invoked.facts,
        )
        return self._StepResult(
            result=result,
            invoked_facts=invoked.facts,
            progress_signature=progress_signature,
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

    def _memory_semaphore(self) -> asyncio.Semaphore:
        if self._memory_sem is None:
            self._memory_sem = asyncio.Semaphore(2)
        return self._memory_sem

    def _conversation_context(self, session_id: str | None) -> str:
        if not session_id:
            return ""
        messages = self.runtime.memory.get_messages(session_id, limit=100)
        return self.context_manager.build_conversation_context(messages)



# Deferred import: execution -> capabilities -> connector_runtime -> engine forms a
# cycle, so we register after HernessEngine is defined to break it.
from .execution import register_engine_class  # noqa: E402

register_engine_class(HernessEngine)
