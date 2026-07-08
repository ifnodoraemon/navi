from __future__ import annotations

import asyncio
import json
import logging
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
from .request_router import ModelRequestRouter, RequestRouter, request_router_contract
from .runtime import AgentRuntime
from .runs import RunStore
from .trace import TraceStore

logger = logging.getLogger("navi.control_plane")

__all__ = ["AgentTurnResult", "TurnController", "_dynamic_intent_facts"]


class TurnController(TurnLifecycleMixin):
    """Thin turn facade for the Navi 2.0 control plane.

    This class intentionally does not contain a ReAct loop. It only normalizes
    an inbound turn, routes it through the RequestRouter contract, invokes
    goal/control capabilities, or returns a one-turn responder answer.
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
        route_payload = _structured_routing_decision(intent_facts)
        if not route_payload and _provider_has_explicit_router(self.runtime.provider):
            try:
                route_decision = await ModelRequestRouter(self.runtime.provider).route(
                    text,
                    current_state=runtime_facts.get("current_state", {}),
                    connector_facts=_dynamic_intent_facts(intent_facts),
                )
                route_payload = route_decision.to_dict()
            except Exception as exc:
                return self._finalize_turn(
                    text,
                    AgentTurnResult(
                        text="",
                        action="execute:system.request_route_error",
                        observation=_fact_event(
                            "request_route_error",
                            {"error_type": type(exc).__name__, "error": str(exc)},
                        ),
                        model_role="request_router",
                        terminal=True,
                        ok=False,
                        error_reason="request_route_error",
                        trace_id=trace_id,
                        facts={"error_type": type(exc).__name__, "error": str(exc)},
                    ),
                    trace_id=trace_id,
                    session_id=resolved_session_id,
                    source=source,
                    peer_id=peer_id,
                    sender_id=sender_id,
                )
            self.trace.add_event(
                trace_id=trace_id,
                phase=TracePhase.AGENT_ROLE_RESULT,
                session_id=resolved_session_id or "",
                source=source,
                peer_id=peer_id,
                sender_id=sender_id,
                model_role="router",
                ok=True,
                input_data={"router_enabled": True},
                output_data=route_payload,
                message="model request router selected route",
            )

        if route_payload:
            routed = await self._handle_route_payload(
                text,
                route_payload,
                trace_id=trace_id,
                session_id=resolved_session_id,
                source=source,
                peer_id=peer_id,
                sender_id=sender_id,
                context=context,
            )
            if routed is not None:
                return routed

        return await self._answer_fast_path(
            text,
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
            "request_router_contract": request_router_contract(),
        }
        dynamic_intent_facts = _dynamic_intent_facts(intent_facts)
        if dynamic_intent_facts:
            runtime_facts["dynamic_intent"] = dynamic_intent_facts
        return resolved_session_id, trace_id, context, runtime_facts

    async def _handle_route_payload(
        self,
        text: str,
        route_payload: dict[str, Any],
        *,
        trace_id: str,
        session_id: str,
        source: str,
        peer_id: str,
        sender_id: str,
        context: CapabilityContext,
    ) -> AgentTurnResult | None:
        try:
            decision = RequestRouter().route_model_decision(route_payload)
        except ValueError as exc:
            return self._finalize_turn(
                text,
                AgentTurnResult(
                    text="",
                    action="execute:system.request_route_error",
                    observation=_fact_event(
                        "request_route_error",
                        {"error_type": type(exc).__name__, "error": str(exc)},
                    ),
                    model_role="request_router",
                    terminal=True,
                    ok=False,
                    error_reason="request_route_error",
                    trace_id=trace_id,
                    facts={"error_type": type(exc).__name__, "error": str(exc)},
                ),
                trace_id=trace_id,
                session_id=session_id,
                source=source,
                peer_id=peer_id,
                sender_id=sender_id,
            )
        routed = _route_to_goal_tool(
            decision.to_dict(),
            user_text=text,
            workspace=context.workspace,
        )
        if routed is None:
            return None
        tool, args, permission = routed
        self.trace.add_event(
            trace_id=trace_id,
            phase=TracePhase.AGENT_ROLE_RESULT,
            session_id=session_id or "",
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            model_role="request_router",
            ok=True,
            input_data={"decision": decision.to_dict()},
            output_data={"tool": tool, "permission": permission},
            message="structured request route accepted",
        )
        invoked = await self.capabilities.invoke(
            tool,
            args,
            permission=permission,
            context=context,
        )
        self.trace.add_event(
            trace_id=trace_id,
            phase=TracePhase.CAPABILITY_RESULT,
            session_id=session_id or "",
            run_id=invoked.run_id,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            tool=tool,
            model_role="request_router",
            ok=invoked.ok,
            input_data={"args": args, "permission": permission},
            output_data={
                "action": invoked.action,
                "facts": invoked.facts or {},
                "terminal": invoked.terminal,
            },
            message=invoked.message,
        )
        surface_text = invoked.message
        if not surface_text and invoked.facts and invoked.yields_control:
            surface_text = await self._response_from_facts(text, invoked.facts)
        result = AgentTurnResult(
            text=surface_text,
            run_id=invoked.run_id,
            action=invoked.action,
            observation=_fact_event(
                "request_route_result",
                {
                    "tool": tool,
                    "ok": invoked.ok,
                    "action": invoked.action,
                    "facts": invoked.facts or {},
                    "error_reason": getattr(invoked, "error_reason", ""),
                },
            ),
            model_role="request_router",
            terminal=True,
            ok=invoked.ok,
            trace_id=trace_id,
            facts=invoked.facts,
            error_reason=getattr(invoked, "error_reason", ""),
            yields_control=invoked.yields_control,
        )
        return self._finalize_turn(
            text,
            result,
            trace_id=trace_id,
            session_id=session_id,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
        )

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

    async def _answer_fast_path(
        self,
        text: str,
        *,
        trace_id: str,
        session_id: str,
        source: str,
        peer_id: str,
        sender_id: str,
    ) -> AgentTurnResult:
        try:
            answer = await self.runtime.complete(
                [
                    ChatMessage(
                        "system",
                        "Answer the user directly. Do not perform tool calls in fast path.",
                    ),
                    ChatMessage("user", text),
                ],
                role="responder",
            )
            result = AgentTurnResult(
                text=answer,
                action="chat",
                model_role="responder",
                terminal=True,
                ok=True,
                trace_id=trace_id,
            )
        except Exception as exc:
            result = AgentTurnResult(
                text="",
                action="execute:system.responder_error",
                observation=_fact_event(
                    "responder_error",
                    {"error_type": type(exc).__name__, "error": str(exc)},
                ),
                model_role="responder",
                terminal=True,
                ok=False,
                error_reason="responder_error",
                trace_id=trace_id,
                facts={"error_type": type(exc).__name__, "error": str(exc)},
            )
        self.trace.add_event(
            trace_id=trace_id,
            phase=TracePhase.AGENT_ROLE_RESULT,
            session_id=session_id or "",
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            model_role=result.model_role,
            ok=result.ok,
            output_data={"action": result.action},
            message=result.surfaced_text()[:1600],
        )
        return self._finalize_turn(
            text,
            result,
            trace_id=trace_id,
            session_id=session_id,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
        )

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


def _structured_routing_decision(intent_facts: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(intent_facts, dict):
        return {}
    for key in ("request_routing_decision", "routing_decision", "route_decision"):
        value = intent_facts.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _provider_has_explicit_router(provider: Any) -> bool:
    if bool(getattr(provider, "enable_request_router", False)):
        return True
    routes = getattr(provider, "routes", None)
    return isinstance(routes, dict) and "router" in routes


def _route_to_goal_tool(
    decision: dict[str, Any],
    *,
    user_text: str,
    workspace: str,
) -> tuple[str, dict[str, Any], str] | None:
    intent = str(decision.get("intent") or "")
    facts = decision.get("facts") if isinstance(decision.get("facts"), dict) else {}
    if intent == "open_goal":
        args = {
            "objective": str(facts.get("objective") or user_text).strip(),
            "workspace": str(facts.get("workspace") or workspace).strip(),
        }
        for key in (
            "scope",
            "constraints",
            "acceptance_criteria",
            "permission_ceiling",
            "allowed_capabilities",
            "verification_command",
            "timeout_seconds",
            "auto_start",
        ):
            if key in facts:
                args[key] = facts[key]
        return ("goal.open", args, "prepare")
    if intent == "resume_goal":
        args = {
            "goal_id": str(decision.get("goal_id") or facts.get("goal_id") or "").strip(),
            "loop_run_id": str(facts.get("loop_run_id") or "").strip(),
            "workspace": str(facts.get("workspace") or workspace).strip(),
        }
        return ("goal.resume", args, "prepare")
    if intent == "control_goal":
        control = str(facts.get("control") or facts.get("action") or "state").strip()
        args = {
            "goal_id": str(decision.get("goal_id") or facts.get("goal_id") or "").strip(),
            "loop_run_id": str(facts.get("loop_run_id") or "").strip(),
        }
        if control in {"cancel", "cancel_goal"}:
            if "reason" in facts:
                args["reason"] = facts["reason"]
            return ("goal.cancel", args, "prepare")
        if control in {"state", "status", "read"} and "limit" in facts:
            args["limit"] = facts["limit"]
        return ("goal.state", args, "read")
    if intent == "request_elevation":
        return (
            "session.request_elevation",
            {
                "target_permission": str(facts.get("target_permission") or "write").strip(),
                "reason": str(facts.get("reason") or user_text).strip(),
            },
            "read",
        )
    return None


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


def _fact_event(kind: str, facts: dict[str, Any]) -> str:
    return json.dumps(
        {
            "fact_type": kind,
            "facts": facts,
        },
        ensure_ascii=False,
        sort_keys=True,
    )



