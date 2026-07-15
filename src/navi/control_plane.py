from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from .turn_lifecycle import TurnLifecycleMixin
from .capabilities import CapabilityContext, CapabilityRegistry
from .control import CurrentStateBuilder, SurfaceContext, current_state_facts
from .turn_result import AgentTurnResult
from .loop import TracePhase
from .operating_context import PERMISSION_ORDER
from .prompt_os import assemble_fact_response_system_prompt, assemble_fact_response_turn_input
from .provider import ChatMessage
from .runtime import AgentRuntime
from .runs import RunStore
from .trace import TraceStore

logger = logging.getLogger("navi.control_plane")

__all__ = ["AgentTurnResult", "TurnController"]


class TurnController(TurnLifecycleMixin):
    """Thin turn facade for the Navi 2.0 control plane.

    This class normalizes inbound turns and hands them to the unified loop
    kernel. Every turn runs through the durable loop machinery; `loop_kind`
    distinguishes ordinary turns from explicit durable goals and control work.
    """

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
            governed_run_id=governed_run_id,
            runtime=runtime,
        )
        self.trace = TraceStore(home)
        self.governed_run_id = governed_run_id or ""
        self.governed_workflow_id = governed_workflow_id or ""
        self._background_tasks: set[asyncio.Task[Any]] = set()

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
        resolved_session_id, trace_id, context, runtime_facts = self._initialize_turn(
            text,
            peer_id,
            sender_id,
            source,
            session_id,
            session_alias,
            intent_facts,
            trace_id,
        )
        # Unified loop path: every turn opens a loop record whose objective is
        # the user's message, then runs the planner ReAct loop. The planner picks
        # capabilities (shell.run, send_file, respond, ...),
        # the executor runs them, and the checker verifies whether the objective
        # is achieved.
        invoked = await self.capabilities.invoke(
            "goal.open",
            {
                "objective": text,
                "workspace": str(self.project_dir.resolve()),
                "loop_kind": "turn",
            },
            permission="prepare",
            context=context,
        )
        self.trace.add_event(
            trace_id=trace_id,
            phase=TracePhase.CAPABILITY_RESULT,
            session_id=resolved_session_id or "",
            run_id=invoked.run_id,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            tool="goal.open",
            model_role="loop_kernel",
            ok=invoked.ok,
            input_data={
                "args": {
                    "objective": text,
                    "workspace": str(self.project_dir.resolve()),
                    "loop_kind": "turn",
                },
                "permission": "prepare",
            },
            output_data={
                "action": invoked.action,
                "facts": invoked.facts or {},
                "terminal": invoked.terminal,
            },
            message=invoked.message,
        )
        invoked_facts = dict(invoked.facts or {})
        surface_text = str(invoked_facts.get("responded_message") or "").strip()
        if not surface_text:
            # Capability messages are machine observations, not user copy.
            # Give the responder both the structured result and the raw
            # observation as facts; never surface the observation directly.
            response_facts = {
                **invoked_facts,
                "capability_result": {
                    "ok": invoked.ok,
                    "action": invoked.action,
                    "error_reason": getattr(invoked, "error_reason", ""),
                    "observation": invoked.message,
                },
            }
            surface_text = await self._response_from_facts(text, response_facts)
        from .connector_delivery import connector_delivery_from_facts

        has_delivery = connector_delivery_from_facts(invoked_facts) is not None
        has_surface_result = bool(surface_text) or has_delivery
        turn_ok = invoked.ok and has_surface_result
        turn_error_reason = getattr(invoked, "error_reason", "")
        if invoked.ok and not has_surface_result:
            turn_error_reason = "empty_response"
        result = AgentTurnResult(
            text=surface_text,
            run_id=invoked.run_id,
            action=invoked.action,
            observation=_fact_event(
                "request_route_result",
                {
                    "tool": "goal.open",
                    "ok": invoked.ok,
                    "action": invoked.action,
                    "facts": invoked.facts or {},
                    "error_reason": getattr(invoked, "error_reason", ""),
                },
            ),
            model_role="loop_kernel",
            terminal=invoked.terminal,
            ok=turn_ok,
            trace_id=trace_id,
            facts=invoked.facts or {},
            error_reason=turn_error_reason,
            yields_control=getattr(invoked, "yields_control", False),
        )
        return self._finalize_turn(
            text,
            result,
            trace_id=trace_id,
            session_id=resolved_session_id,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
        )

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
    ) -> tuple[str, str, CapabilityContext, dict[str, Any]]:
        resolved_session_id = session_id
        if not resolved_session_id and session_alias:
            resolved_session_id = self.runtime.memory.current_session_id(session_alias)
        resolved_session_id = resolved_session_id or ""
        trace_id = trace_id or self.trace.new_trace_id()
        self.trace.add_event(
            trace_id=trace_id,
            phase=TracePhase.TURN_START,
            session_id=resolved_session_id,
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
            allowed_tools=(
                frozenset(self.capabilities.allowed_tools)
                if self.capabilities.allowed_tools is not None
                else None
            ),
            disabled_tools=frozenset(self.capabilities.disabled_tools),
            disabled_capability_classes=frozenset(
                self.capabilities.disabled_capability_classes
            ),
        )
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
        runtime_facts: dict[str, Any] = {
            "current_state": current_state_facts(current_state),
        }
        if intent_facts:
            runtime_facts["intent_facts"] = dict(intent_facts)
        context = replace(context, runtime_facts=runtime_facts)
        return resolved_session_id, trace_id, context, runtime_facts

    async def _response_from_facts(self, user_text: str, facts: dict[str, Any]) -> str:
        try:
            return await self.runtime.complete(
                [
                    ChatMessage("system", assemble_fact_response_system_prompt().render()),
                    ChatMessage(
                        "user",
                        assemble_fact_response_turn_input(
                            user_text=user_text,
                            facts=facts,
                        ).render(),
                    ),
                ],
                role="responder",
            )
        except Exception:
            logger.exception("failed to synthesize user-facing response from facts")
            return ""

    def _finalize_turn(
        self,
        user_text: str,
        result: AgentTurnResult,
        *,
        trace_id: str,
        session_id: str,
        source: str,
        peer_id: str,
        sender_id: str,
    ) -> AgentTurnResult:
        turn_res = self._record_turn(user_text, result, session_id=session_id)
        turn_res = self._with_trace(turn_res, trace_id)
        goal_id = str((turn_res.facts or {}).get("goal_id") or "").strip()
        if goal_id:
            self._attach_goals(
                {goal_id},
                trace_id=trace_id,
                session_id=turn_res.session_id,
                evidence={"final_action": turn_res.action},
            )
        self._record_trace_final(turn_res, trace_id, source=source, peer_id=peer_id, sender_id=sender_id)
        self._trigger_background_memory(turn_res)
        return turn_res

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

    async def shutdown(self, *, timeout: float = 10.0) -> None:
        if self.event_bus:
            try:
                await asyncio.wait_for(self.event_bus.drain(), timeout=5.0)
                await asyncio.wait_for(self.event_bus.shutdown(), timeout=5.0)
            except Exception as exc:
                logger.error(
                    "Failed to drain/shutdown event bus during engine shutdown: %s",
                    exc,
                    exc_info=True,
                )
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


def _max_permission(current: str, requested: str) -> str:
    current_level = PERMISSION_ORDER.get(current, 0)
    requested_level = PERMISSION_ORDER.get(requested, 0)
    return requested if requested_level > current_level else current


def _fact_event(kind: str, facts: dict[str, Any]) -> str:
    return json.dumps(
        {
            "fact_type": kind,
            "facts": facts,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
