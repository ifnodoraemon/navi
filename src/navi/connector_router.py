from __future__ import annotations

import asyncio
import re
from pathlib import Path

from .connector_runtime import ConnectorMessage
from .event_bus import (
    EventBus,
    MessageIngressEvent,
    ResponseReadyEvent,
)
from .prompt_os import (
    assemble_fact_response_system_prompt,
    assemble_fact_response_turn_input,
)
from .provider import ChatMessage

# Idle window: how long we tolerate *silence* on the response channel before
# declaring the upstream unresponsive. A turn that is still working sends
# heartbeats well within this window, so a slow-but-live turn never times out.
# Only a genuinely stuck/crashed upstream (no heartbeat, no response) trips it.
IDLE_TIMEOUT_SECONDS = 120.0


class ConnectorRouter:
    def __init__(self, home: Path, event_bus: EventBus, *, runtime=None) -> None:
        self.home = home
        self.event_bus = event_bus
        self.runtime = runtime

    async def route(self, message: ConnectorMessage) -> "ResponseReadyEvent | None":
        if not message.text.strip():
            return None
        correlation_id = message.message_id
        channel = self.event_bus.create_response_channel(correlation_id)

        from navi.trace import TraceStore
        from navi.loop import TracePhase
        trace = TraceStore(self.home)

        trace.add_event(
            trace_id=correlation_id,
            phase=TracePhase.CHANNEL_INGRESS,
            run_id="",
            source=message.source,
            peer_id=message.peer_id,
            sender_id=message.sender_id,
            input_data={"text": message.text, "facts": message.facts},
            message="Received message from channel",
        )

        try:
            control_response = await self._resolve_connector_control_message(message)
            if control_response is not None:
                trace.add_event(
                    trace_id=correlation_id,
                    phase=TracePhase.RESPONSE_READY,
                    run_id="",
                    source=message.source,
                    peer_id=message.peer_id,
                    sender_id=message.sender_id,
                    output_data={
                        "response": control_response.text,
                        "action": control_response.action,
                        "control_message": True,
                    },
                    message="Resolved connector control message",
                )
                return control_response

            event = MessageIngressEvent(
                source_agent="connector_router",
                correlation_id=correlation_id,
                message_id=message.message_id,
                peer_id=message.peer_id,
                sender_id=message.sender_id,
                text=message.text,
                source=message.source,
                session_alias=message.session_alias,
                facts=message.facts,
            )

            await self.event_bus.publish(event)
            response = await self._await_response(channel, correlation_id=correlation_id)

            if response:
                trace.add_event(
                    trace_id=correlation_id,
                    phase=TracePhase.RESPONSE_READY,
                    run_id="",
                    source=message.source,
                    peer_id=message.peer_id,
                    sender_id=message.sender_id,
                    output_data={"response": response.text, "action": response.action},
                    message="Prepared response for channel delivery",
                )
            return response
        except Exception as e:
            trace.add_event(
                trace_id=correlation_id,
                phase=TracePhase.RESPONSE_READY,
                run_id="",
                source=message.source,
                peer_id=message.peer_id,
                sender_id=message.sender_id,
                output_data={"error": str(e)},
                message="Failed to prepare channel response",
                ok=False,
            )
            raise
        finally:
            self.event_bus.remove_response_channel(correlation_id)

    async def _await_response(self, channel: asyncio.Queue, *, correlation_id: str) -> "ResponseReadyEvent | None":
        """Wait for the real response, resetting the deadline on every heartbeat.

        The timeout is per-idle-gap, not a total wall clock: as long as the turn
        keeps signalling liveness, we keep waiting. Silence longer than the idle
        window means the upstream is stuck, so we surface a timeout."""
        while True:
            try:
                item = await asyncio.wait_for(channel.get(), timeout=IDLE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                return None
            if isinstance(item, ResponseReadyEvent):
                return item
            # Heartbeat (or any non-terminal signal): upstream is alive, keep waiting.

    async def _resolve_connector_control_message(
        self, message: ConnectorMessage
    ) -> "ResponseReadyEvent | None":
        command = _parse_connector_approval_command(message)
        if command is None:
            return None
        decision, code = command
        from .control import ApprovalService, SurfaceContext

        result = await ApprovalService(self.home).resolve_and_continue(
            decision=decision,
            selection="explicit_code",
            context=SurfaceContext(
                home=self.home,
                source=message.source,
                peer_id=message.peer_id,
                sender_id=message.sender_id,
                input_text=message.text,
            ),
            code=code,
            runtime=self.runtime,
            trace_id=message.message_id,
            event_bus=self.event_bus,
        )
        from .connector_delivery import connector_delivery_from_facts

        delivery = connector_delivery_from_facts(result.facts)
        if delivery is not None:
            return ResponseReadyEvent(
                source_agent="router",
                text=delivery.text,
                source=message.source,
                peer_id=message.peer_id,
                sender_id=message.sender_id,
                action="connector_outbound",
                facts=result.facts,
            )
        # A resumed action may stop at a new approval gate.  Its
        # ``surface_message`` is deterministic control-plane output carrying
        # the exact new code.  Returning it directly prevents a responder
        # model from paraphrasing away that code and making the task
        # impossible to continue.
        surface_message = str(result.facts.get("surface_message") or "").strip()
        if surface_message:
            return ResponseReadyEvent(
                source_agent="router",
                text=surface_message,
                source=message.source,
                peer_id=message.peer_id,
                sender_id=message.sender_id,
                facts=result.facts,
            )
        if self.runtime is None or not _approval_result_needs_model_response(result.facts):
            return ResponseReadyEvent(
                source_agent="router",
                text=result.message,
                source=message.source,
                peer_id=message.peer_id,
                sender_id=message.sender_id,
                facts=result.facts,
            )
        try:
            text = await self.runtime.complete(
                [
                    ChatMessage("system", assemble_fact_response_system_prompt().render()),
                    ChatMessage(
                        "user",
                        assemble_fact_response_turn_input(
                            user_text=message.text,
                            facts={"approval_control": result.facts},
                        ).render(),
                    ),
                ],
                role="responder",
            )
        except Exception:
            text = result.message
        return ResponseReadyEvent(
            source_agent="router",
            text=text,
            source=message.source,
            peer_id=message.peer_id,
            sender_id=message.sender_id,
            facts=result.facts,
        )


def _approval_result_needs_model_response(facts: dict[str, object]) -> bool:
    """Keep approval state deterministic; model-synthesize resumed task outcomes."""
    continuation_status = str(facts.get("continuation_status") or "")
    return bool(facts.get("loop_run_id")) and continuation_status not in {
        "",
        "queued",
        "unavailable",
    }


def _parse_connector_approval_command(message: ConnectorMessage) -> tuple[str, str] | None:
    # This is a control-envelope check, not natural-language intent parsing.
    # Only a connector-declared command plus an approval code may bypass the
    # model loop; every other message remains model-owned user intent.
    spec = _connector_spec_for_source(message.source)
    if spec is None:
        return None
    code = _approval_code_for_declared_command(
        message.text or "",
        spec.approval_approve_commands,
    )
    if code:
        return ("approve", code)
    code = _approval_code_for_declared_command(
        message.text or "",
        spec.approval_reject_commands,
    )
    if code:
        return ("reject", code)
    return None


def _approval_code_for_declared_command(text: str, commands: tuple[str, ...]) -> str | None:
    for command in sorted((item.strip() for item in commands if item.strip()), key=len, reverse=True):
        pattern = rf"\s*[`'\"]?{re.escape(command)}[`'\"]?\s*[`'\"]?([0-9]{{6}})[`'\"]?\s*"
        match = re.fullmatch(pattern, text)
        if match:
            return match.group(1)
    return None


def _connector_spec_for_source(source: str):
    from .connector_registry import load_connector_adapters

    raw = source.strip()
    for adapter in load_connector_adapters():
        spec = adapter.spec
        if raw in {spec.name, spec.surface, spec.local_source}:
            return spec
    return None
