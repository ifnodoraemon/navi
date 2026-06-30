from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, FrozenSet

from .engine import HernessEngine
from .runtime import AgentRuntime

if TYPE_CHECKING:
    from .event_bus import EventBus


# How often a still-running turn signals liveness on its response channel. Must
# be comfortably below the router's IDLE_TIMEOUT_SECONDS so a live turn never
# trips the idle timeout between two heartbeats.
HEARTBEAT_INTERVAL_SECONDS = 20.0


# The remote-connector security boundary is an explicit preparation/read
# allowlist. Remote surfaces can converse, ask, propose or inspect governed
# work, and create prepared delegation/watch state. New mutating capabilities do
# not become remotely visible unless the policy names them.
REMOTE_ALLOWED_TOOLS = frozenset(
    (
        "final.answer",
        "ask.user",
        "delegate.spawn",
        "delegate.prepare",
        "delegate.list",
        "tools.list",
        "watch.create",
        "workflow.propose",
        "workflow.status",
    )
)

REMOTE_BLOCKED_CAPABILITY_CLASSES = frozenset(
    (
        "browser",
        "codebase",
        "directory",
        "file.read",
        "file.write",
        "git",
        "shell",
        "service",
        "system",
        "test",
        "watch.delete",
    )
)

REMOTE_BLOCKED_TOOLS = frozenset(
    (
        "workflow.approve",
        "workflow.run",
    )
)


@dataclass(frozen=True)
class ConnectorToolPolicy:
    """Inspectable remote-surface capability policy."""

    name: str
    permission_ceiling: str
    allowed_tools: FrozenSet[str]
    blocked_tools: FrozenSet[str]
    blocked_capability_classes: FrozenSet[str]
    reason_code: str

    def allowed_tool_names(self) -> set[str] | None:
        if not self.allowed_tools:
            return None
        return set(self.allowed_tools)

    def facts(self) -> dict:
        return {
            "name": self.name,
            "permission_ceiling": self.permission_ceiling,
            "allowed_tools": sorted(self.allowed_tools),
            "blocked_tools": sorted(self.blocked_tools),
            "blocked_capability_classes": sorted(self.blocked_capability_classes),
            "reason_code": self.reason_code,
        }


REMOTE_CONNECTOR_TOOL_POLICY = ConnectorToolPolicy(
    name="remote_connector_default",
    permission_ceiling="prepare",
    allowed_tools=REMOTE_ALLOWED_TOOLS,
    blocked_tools=REMOTE_BLOCKED_TOOLS,
    blocked_capability_classes=REMOTE_BLOCKED_CAPABILITY_CLASSES,
    reason_code="remote_connector_policy",
)

LOCAL_CONVERSATIONAL_TOOL_POLICY = ConnectorToolPolicy(
    name="local_conversational_default",
    permission_ceiling="write",
    allowed_tools=frozenset(),
    blocked_tools=frozenset(),
    blocked_capability_classes=frozenset(),
    reason_code="local_conversational_policy",
)


@dataclass(frozen=True)
class ConnectorMessage:
    message_id: str
    peer_id: str
    sender_id: str
    text: str
    source: str
    session_alias_prefix: str
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def session_alias(self) -> str:
        return f"{self.session_alias_prefix}:{self.peer_id}"

    @property
    def content_key(self) -> str:
        payload = json.dumps(
            {"text": self.text, "facts": self.facts},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        digest = hashlib.md5(payload.encode()).hexdigest()
        return f"content:{self.sender_id}:{digest}"


class ConnectorIngressRuntime:
    """Shared IM ingress path into the agent kernel via event bus."""

    def __init__(
        self,
        *,
        home: Path,
        runtime: AgentRuntime,
        project_dir: Path,
        allow_sources: set[str] | None = None,
        allowed_tools: set[str] | None = None,
        tool_policy: ConnectorToolPolicy = REMOTE_CONNECTOR_TOOL_POLICY,
        event_bus: "EventBus | None" = None,
    ):
        from .connector_router import ConnectorRouter
        from .event_bus import EventBus as _EventBus

        self.tool_policy = tool_policy
        self.event_bus = event_bus or _EventBus()
        self.router = ConnectorRouter(home, self.event_bus)
        self.agent = HernessEngine(
            home=home,
            runtime=runtime,
            project_dir=project_dir,
            allow_sources=allow_sources,
            allowed_tools=tool_policy.allowed_tool_names()
            if allowed_tools is None
            else allowed_tools,
            disabled_tools=tool_policy.blocked_tools,
            disabled_capability_classes=tool_policy.blocked_capability_classes,
            permission_ceiling=tool_policy.permission_ceiling,
            event_bus=self.event_bus,
        )
        self._setup_event_subscriptions()

    def _setup_event_subscriptions(self) -> None:
        from .event_bus import ResponseReadyEvent, UserIntentEvent
        from .governance_agent import GovernanceAgent
        from .intent_agent import IntentAgent

        self._governance = GovernanceAgent(self.agent.home, self.event_bus)
        self._intent = IntentAgent(self.agent.home, self.agent.runtime, self.event_bus)

        async def on_user_intent(event: UserIntentEvent) -> None:
            text = await self._handle_with_heartbeat(event)
            await self.event_bus.send_response(
                ResponseReadyEvent(
                    source_agent="main_agent",
                    correlation_id=event.correlation_id,
                    peer_id=event.peer_id,
                    sender_id=event.sender_id,
                    text=text,
                    source=event.source,
                )
            )

        self.event_bus.subscribe("user_intent", on_user_intent)

        from .event_bus import RunCompletedEvent, ActionSuspendedEvent

        async def on_action_suspended(event: ActionSuspendedEvent) -> None:
            text = f"action_suspended\nrun_id={event.run_id}\napproval_code={event.approval_code}"
            await self.event_bus.send_response(
                ResponseReadyEvent(
                    source_agent="governance_agent",
                    correlation_id=event.correlation_id,
                    peer_id=event.peer_id,
                    sender_id=event.sender_id,
                    text=text,
                    source=event.source,
                )
            )

        self.event_bus.subscribe("action_suspended", on_action_suspended)

        async def on_run_completed(event: RunCompletedEvent) -> None:
            if event.status == "failed":
                text = (
                    "delegated_subtask_completed\n"
                    f"run_id={event.run_id}\n"
                    f"status={event.status}\n"
                    f"error={event.error or ''}"
                ).strip()
                await self.event_bus.send_response(
                    ResponseReadyEvent(
                        source_agent="runtime",
                        correlation_id=event.correlation_id,
                        peer_id=event.peer_id,
                        sender_id=event.sender_id,
                        text=text,
                        source="runtime",
                    )
                )

        self.event_bus.subscribe("run_completed", on_run_completed)

    async def _handle_with_heartbeat(self, event) -> str:
        """Run the agent turn while emitting heartbeats so a slow-but-live turn
        is never mistaken for a stuck upstream by the router's idle timeout.

        The handler runs as a task; a companion loop pings the response channel
        until it finishes. Any handler failure is turned into a user-facing
        message rather than left to hang the channel."""
        handler_task = asyncio.ensure_future(
            self.agent.handle(
                event.text,
                peer_id=event.peer_id,
                sender_id=event.sender_id,
                source=event.source,
                session_alias=event.session_alias,
                intent_facts=event.facts,
                trace_id=event.correlation_id,
            )
        )

        async def beat() -> None:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                await self.event_bus.send_heartbeat(event.correlation_id)

        heartbeat_task = asyncio.ensure_future(beat())
        try:
            result = await handler_task
            return result.surfaced_text()
        except Exception as exc:
            import logging

            logging.getLogger("navi.connector").error(
                "Agent turn failed for correlation %s: %s",
                event.correlation_id,
                exc,
                exc_info=True,
            )
            return (
                "连接器处理失败；"
                f"correlation_id={event.correlation_id}；"
                f"error_type={type(exc).__name__}。"
            )
        finally:
            heartbeat_task.cancel()

    async def handle(self, message: ConnectorMessage) -> str:
        text = await self.router.route(message)
        from .safeguards import redact_secrets

        return redact_secrets(text)
