from __future__ import annotations

import logging
from pathlib import Path

from .conversation_contract import CONVERSATION_ACTION_ASK
from .control import CurrentStateBuilder, SurfaceContext, current_state_facts
from .event_bus import (
    AgentTurnCompletedEvent,
    EventBus,
    MessageIngressEvent,
    NaviEvent,
    UserIntentEvent,
)
from .runtime import AgentRuntime

logger = logging.getLogger("navi.intent")


class IntentAgent:
    """Collect dynamic intent facts without classifying user language into modes."""

    def __init__(self, home: Path, runtime: AgentRuntime, event_bus: EventBus) -> None:
        self.home = home
        self.runtime = runtime
        self.event_bus = event_bus
        self._pending_asks: dict[str, bool] = {}
        self._subscribe()

    def _subscribe(self) -> None:
        self.event_bus.subscribe("message_ingress", self._on_message_ingress)
        self.event_bus.subscribe("agent_turn_completed", self._on_turn_completed)

    async def _on_turn_completed(self, event: NaviEvent) -> None:
        assert isinstance(event, AgentTurnCompletedEvent)
        if event.action == CONVERSATION_ACTION_ASK:
            self._pending_asks[event.session_id] = True
        else:
            self._pending_asks.pop(event.session_id, None)

    async def _on_message_ingress(self, event: NaviEvent) -> None:
        assert isinstance(event, MessageIngressEvent)
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
        current_state = current_state_facts(state)
        facts = {
            "source_agent": "intent_agent",
            "intent_basis": "current_state_facts",
            "current_state": current_state,
        }
        if event.facts:
            facts["connector_message"] = event.facts

        if session_id and self._pending_asks.pop(session_id, False):
            messages = self.runtime.memory.get_messages(session_id, limit=2)
            if messages and messages[-1].role == "assistant":
                facts["pending_ask"] = {
                    "type": "ask_reply_context",
                    "last_assistant_message_preview": messages[-1].content[:300],
                }

        logger.info(
            "Published dynamic intent facts for message %s: runs=%s workflows=%s",
            event.message_id,
            len(current_state["active_runs"]),
            len(current_state["active_workflows"]),
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
