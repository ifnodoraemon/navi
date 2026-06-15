from __future__ import annotations

import asyncio
from pathlib import Path

from .connector_runtime import ConnectorMessage
from .event_bus import (
    EventBus,
    MessageIngressEvent,
)


class ConnectorRouter:
    def __init__(self, home: Path, event_bus: EventBus) -> None:
        self.home = home
        self.event_bus = event_bus

    async def route(self, message: ConnectorMessage) -> str:
        correlation_id = message.message_id
        channel = self.event_bus.create_response_channel(correlation_id)

        try:
            event = MessageIngressEvent(
                source_agent="connector_router",
                correlation_id=correlation_id,
                message_id=message.message_id,
                peer_id=message.peer_id,
                sender_id=message.sender_id,
                text=message.text,
                source=message.source,
                session_alias=message.session_alias,
            )

            await self.event_bus.publish(event)

            try:
                response = await asyncio.wait_for(channel.get(), timeout=120.0)
                return response.text
            except asyncio.TimeoutError:
                return "处理超时，请稍后重试。"
        finally:
            self.event_bus.remove_response_channel(correlation_id)
