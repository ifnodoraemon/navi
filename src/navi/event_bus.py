from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class NaviEvent:
    event_type: str = ""
    source_agent: str = ""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = field(default_factory=time.time)
    correlation_id: str = ""


# ─── Message Ingress ───


@dataclass(frozen=True)
class MessageIngressEvent(NaviEvent):
    event_type: str = "message_ingress"
    message_id: str = ""
    peer_id: str = ""
    sender_id: str = ""
    text: str = ""
    source: str = ""
    session_alias: str = ""


# ─── Router → Agents ───


@dataclass(frozen=True)
class UserIntentEvent(NaviEvent):
    event_type: str = "user_intent"
    message_id: str = ""
    peer_id: str = ""
    sender_id: str = ""
    text: str = ""
    source: str = ""
    session_alias: str = ""
    session_id: str = ""
    facts: dict[str, Any] = field(default_factory=dict)


# ─── Main Agent → Governance ───


@dataclass(frozen=True)
class ActionRequestedEvent(NaviEvent):
    event_type: str = "action_requested"
    run_id: str = ""
    peer_id: str = ""
    sender_id: str = ""
    source: str = ""
    autonomy_level: str = ""


# ─── Governance → Execution ───


@dataclass(frozen=True)
class AgentTurnCompletedEvent(NaviEvent):
    event_type: str = "agent_turn_completed"
    session_id: str = ""
    run_id: str = ""
    action: str = ""


@dataclass(frozen=True)
class RunCompletedEvent(NaviEvent):
    event_type: str = "run_completed"
    run_id: str = ""
    status: str = ""
    error: str = ""
    peer_id: str = ""
    sender_id: str = ""


@dataclass(frozen=True)
class ActionApprovedEvent(NaviEvent):
    event_type: str = "action_approved"
    run_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ActionSuspendedEvent(NaviEvent):
    event_type: str = "action_suspended"
    run_id: str = ""
    reason: str = ""
    approval_code: str = ""
    peer_id: str = ""
    sender_id: str = ""
    source: str = ""


@dataclass(frozen=True)
class ApprovalResolvedEvent(NaviEvent):
    event_type: str = "approval_resolved"
    run_id: str = ""
    approval_id: str = ""
    decision: str = ""
    sender_id: str = ""


# ─── Response ───


@dataclass(frozen=True)
class ResponseReadyEvent(NaviEvent):
    event_type: str = "response_ready"
    message_id: str = ""
    peer_id: str = ""
    sender_id: str = ""
    text: str = ""
    source: str = ""


Handler = Callable[[NaviEvent], Awaitable[None]]
Unsubscribe = Callable[[], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}
        self._response_channels: dict[str, asyncio.Queue[ResponseReadyEvent]] = {}
        self._event_log: list[NaviEvent] = []

    def subscribe(self, event_type: str, handler: Handler) -> Unsubscribe:
        self._handlers.setdefault(event_type, []).append(handler)

        def unsub() -> None:
            handlers = self._handlers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)

        return unsub

    async def publish(self, event: NaviEvent) -> None:
        self._event_log.append(event)
        handlers = self._handlers.get(event.event_type, [])
        for handler in list(handlers):
            await handler(event)

    def create_response_channel(self, correlation_id: str) -> asyncio.Queue[ResponseReadyEvent]:
        q: asyncio.Queue[ResponseReadyEvent] = asyncio.Queue()
        self._response_channels[correlation_id] = q
        return q

    def remove_response_channel(self, correlation_id: str) -> None:
        self._response_channels.pop(correlation_id, None)

    async def send_response(self, event: ResponseReadyEvent) -> None:
        await self.publish(event)
        channel = self._response_channels.get(event.correlation_id)
        if channel:
            await channel.put(event)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    @property
    def log(self) -> list[NaviEvent]:
        return list(self._event_log)
