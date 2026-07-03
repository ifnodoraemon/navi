from __future__ import annotations
from navi.lifecycle import Phase, Governance, Acceptance, Resolution

import asyncio
import re
from pathlib import Path

from .connector_runtime import ConnectorMessage, connector_fact_text
from .event_bus import (
    EventBus,
    MessageIngressEvent,
    ResponseReadyEvent,
)

# Idle window: how long we tolerate *silence* on the response channel before
# declaring the upstream unresponsive. A turn that is still working sends
# heartbeats well within this window, so a slow-but-live turn never times out.
# Only a genuinely stuck/crashed upstream (no heartbeat, no response) trips it.
IDLE_TIMEOUT_SECONDS = 120.0


class ConnectorRouter:
    def __init__(self, home: Path, event_bus: EventBus) -> None:
        self.home = home
        self.event_bus = event_bus

    async def route(self, message: ConnectorMessage) -> str:
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
            control_response = self._resolve_connector_control_message(message)
            if control_response is not None:
                trace.add_event(
                    trace_id=correlation_id,
                    phase=TracePhase.CHANNEL_EGRESS,
                    run_id="",
                    source=message.source,
                    peer_id=message.peer_id,
                    sender_id=message.sender_id,
                    output_data={"response": control_response, "control_message": True},
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

            trace.add_event(
                trace_id=correlation_id,
                phase=TracePhase.CHANNEL_EGRESS,
                run_id="",
                source=message.source,
                peer_id=message.peer_id,
                sender_id=message.sender_id,
                output_data={"response": response},
                message="Sent response to channel",
            )
            return response
        except Exception as e:
            trace.add_event(
                trace_id=correlation_id,
                phase=TracePhase.CHANNEL_EGRESS,
                run_id="",
                source=message.source,
                peer_id=message.peer_id,
                sender_id=message.sender_id,
                output_data={"error": str(e)},
                message="Error processing message",
                ok=False,
            )
            raise
        finally:
            self.event_bus.remove_response_channel(correlation_id)

    async def _await_response(self, channel: asyncio.Queue, *, correlation_id: str) -> str:
        """Wait for the real response, resetting the deadline on every heartbeat.

        The timeout is per-idle-gap, not a total wall clock: as long as the turn
        keeps signalling liveness, we keep waiting. Silence longer than the idle
        window means the upstream is stuck, so we surface a timeout."""
        while True:
            try:
                item = await asyncio.wait_for(channel.get(), timeout=IDLE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                return connector_fact_text(
                    "connector_response_timeout",
                    {"correlation_id": correlation_id},
                )
            if isinstance(item, ResponseReadyEvent):
                return item.text
            # Heartbeat (or any non-terminal signal): upstream is alive, keep waiting.

    def _resolve_connector_control_message(self, message: ConnectorMessage) -> str | None:
        command = _parse_connector_approval_command(message)
        if command is None:
            return None
        decision, code = command
        from .control import ApprovalService, SurfaceContext

        result = ApprovalService(self.home).resolve(
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
        )
        return result.message


def _parse_connector_approval_command(message: ConnectorMessage) -> tuple[str, str] | None:
    # This is a control-envelope check, not natural-language intent parsing.
    # Only a connector-declared command plus an approval code may bypass the
    # model loop; every other message remains model-owned user intent.
    spec = _connector_spec_for_source(message.source)
    if spec is None:
        return None
    match = re.fullmatch(r"\s*(\S+)\s+[`'\"]?([0-9]{6})[`'\"]?\s*", message.text or "")
    if not match:
        return None
    raw_command, code = match.groups()
    command = raw_command.strip("`'\"").lower()
    approve = {item.lower() for item in spec.approval_approve_commands}
    reject = {item.lower() for item in spec.approval_reject_commands}
    if command in approve:
        return ("approve", code)
    if command in reject:
        return ("reject", code)
    return None


def _connector_spec_for_source(source: str):
    from .connector_registry import load_connector_adapters

    raw = source.strip()
    for adapter in load_connector_adapters():
        spec = adapter.spec
        if raw in {spec.name, spec.surface, spec.local_source}:
            return spec
    return None
