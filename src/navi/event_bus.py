from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger("navi.event_bus")


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
    facts: dict[str, Any] = field(default_factory=dict)


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
    phase: str = ""
    resolution: str = ""
    error: str = ""
    peer_id: str = ""
    sender_id: str = ""


@dataclass(frozen=True)
class RunSuspendedEvent(NaviEvent):
    event_type: str = "run_suspended"
    run_id: str = ""
    text: str = ""
    peer_id: str = ""
    sender_id: str = ""
    source: str = ""


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
    action: str = "chat"
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HeartbeatEvent(NaviEvent):
    """Liveness signal pushed onto a response channel while a handler is still
    working. It resets the router's idle timeout so an in-progress turn is never
    mistaken for an unresponsive upstream."""

    event_type: str = "heartbeat"


@dataclass(frozen=True)
class ScheduledTaskEvent(NaviEvent):
    event_type: str = "scheduled_task"
    action: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[NaviEvent], Awaitable[None]]
Unsubscribe = Callable[[], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}
        self._response_channels: dict[str, asyncio.Queue[NaviEvent]] = {}
        self._event_log: list[NaviEvent] = []

        # Asyncio structures initialized lazily for loop-safety
        self._queue: asyncio.Queue[NaviEvent] | None = None
        self._worker_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _ensure_worker(self) -> None:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        if self._queue is None or self._loop != current_loop:
            if self._worker_task and not self._worker_task.done():
                self._worker_task.cancel()
            
            self._loop = current_loop
            self._queue = asyncio.Queue()
            self._worker_task = current_loop.create_task(self._worker_loop())

    async def _worker_loop(self) -> None:
        while True:
            try:
                if self._queue is None:
                    await asyncio.sleep(0.01)
                    continue
                event = await self._queue.get()
            except asyncio.CancelledError:
                break
            except (GeneratorExit, RuntimeError) as e:
                if isinstance(e, GeneratorExit) or "Event loop is closed" in str(e):
                    break
                raise
            except Exception as e:
                logger.error(f"Event bus worker queue.get error: {e}", exc_info=True)
                try:
                    await asyncio.sleep(0.1)
                except (asyncio.CancelledError, GeneratorExit, RuntimeError):
                    break
                continue

            try:
                handlers = self._handlers.get(event.event_type, [])
                if handlers:
                    tasks = [self._run_handler_safe(h, event) for h in list(handlers)]
                    await asyncio.gather(*tasks, return_exceptions=True)
            except Exception as e:
                logger.error(f"Event bus worker error processing event {event}: {e}", exc_info=True)
            finally:
                if self._queue is not None:
                    self._queue.task_done()

    async def _run_handler_safe(self, handler: Handler, event: NaviEvent) -> None:
        try:
            await handler(event)
        except Exception as e:
            logger.error(f"Error executing handler {handler} for event {event}: {e}", exc_info=True)

    def subscribe(self, event_type: str, handler: Handler) -> Unsubscribe:
        self._handlers.setdefault(event_type, []).append(handler)
        self._ensure_worker()

        def unsub() -> None:
            handlers = self._handlers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)

        return unsub

    async def publish(self, event: NaviEvent) -> None:
        self._event_log.append(event)
        self._ensure_worker()
        if self._queue is not None:
            await self._queue.put(event)

    def create_response_channel(self, correlation_id: str) -> asyncio.Queue[NaviEvent]:
        q: asyncio.Queue[NaviEvent] = asyncio.Queue()
        self._response_channels[correlation_id] = q
        return q

    def remove_response_channel(self, correlation_id: str) -> None:
        self._response_channels.pop(correlation_id, None)

    async def send_response(self, event: ResponseReadyEvent) -> None:
        await self.publish(event)
        channel = self._response_channels.get(event.correlation_id)
        if channel:
            await channel.put(event)

    async def broadcast_proactive(self, event: ResponseReadyEvent) -> None:
        """Send a proactive message to all waiting response channels.

        Used when a background delegation run suspends and needs to push
        a question to whichever connector is listening."""
        await self.publish(event)
        for channel in self._response_channels.values():
            await channel.put(event)

    async def send_heartbeat(self, correlation_id: str) -> None:
        """Push a liveness signal onto a waiting response channel, if any.

        No-op when the channel is gone (already responded or removed), so callers
        can fire heartbeats freely without races on completion."""
        channel = self._response_channels.get(correlation_id)
        if channel:
            await channel.put(HeartbeatEvent(correlation_id=correlation_id))

    async def drain(self) -> None:
        self._ensure_worker()
        if self._queue is not None:
            await self._queue.join()

    async def shutdown(self) -> None:
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error during event bus worker shutdown: {e}", exc_info=True)

    @property
    def log(self) -> list[NaviEvent]:
        return list(self._event_log)
