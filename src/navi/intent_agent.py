from __future__ import annotations

import logging
from pathlib import Path

from .control import CurrentStateBuilder, SurfaceContext, current_state_facts
from .event_bus import EventBus, MessageIngressEvent, UserIntentEvent
from .runtime import AgentRuntime

logger = logging.getLogger("navi.intent")


class IntentAgent:
    """Collect dynamic intent facts without classifying user language into modes."""

    def __init__(self, home: Path, runtime: AgentRuntime, event_bus: EventBus) -> None:
        self.home = home
        self.runtime = runtime
        self.event_bus = event_bus
        self._subscribe()

    def _subscribe(self) -> None:
        self.event_bus.subscribe("message_ingress", self._on_message_ingress)

    async def _on_message_ingress(self, event: MessageIngressEvent) -> None:
        session_id = (
            self.runtime.memory.current_session_id(event.session_alias)
            if event.session_alias
            else ""
        )
        state = CurrentStateBuilder(self.home).build(
            SurfaceContext(
                home=self.home,
                source=event.source,
                peer_id=event.peer_id,
                sender_id=event.sender_id,
                session_id=session_id,
                input_text=event.text,
            )
        )
        facts = {
            "source_agent": "intent_agent",
            "intent_basis": "current_state_facts",
            "current_state": current_state_facts(state),
        }
        logger.info(
            "Published dynamic intent facts for message %s: approvals=%s runs=%s workflows=%s",
            event.message_id,
            facts["current_state"]["visible_pending_approval_count"],
            len(facts["current_state"]["active_runs"]),
            len(facts["current_state"]["active_workflows"]),
        )
        await self.event_bus.publish(
            UserIntentEvent(
                source_agent="intent_agent",
                correlation_id=event.correlation_id,
                message_id=event.message_id,
                peer_id=event.peer_id,
                sender_id=event.sender_id,
                text=event.text,
                source=event.source,
                session_alias=event.session_alias,
                session_id=session_id,
                facts=facts,
            )
        )
