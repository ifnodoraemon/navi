from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, FrozenSet

from .engine import HernessEngine
from .runtime import AgentRuntime

if TYPE_CHECKING:
    from .event_bus import EventBus


REMOTE_SAFE_TOOLS = frozenset(
    (
        "final.answer",
        "ask.user",
        "delegate.spawn",
        "delegate.prepare",
        "approval.request",
        "delegate.run",
        "watch.create",
        "approval.resolve",
        "delegate.delete",
        "session.request_elevation",
        "provider.config",
        "service.status",
        "delegate.status",
        "delegate.list",
        "skills.list",
        "skills.view",
        "tools.list",
        "memory.list",
        "memory.recall",
        "workflow.propose",
        "workflow.status",
    )
)
REMOTE_BLOCKED_CAPABILITY_CLASSES = frozenset(
    (
        "browser",
        "directory",
        "file.read",
        "file.write",
        "git",
        "shell",
        "test",
        "watch.delete",
    )
)


@dataclass(frozen=True)
class ConnectorToolPolicy:
    """Inspectable remote-surface capability policy."""

    name: str
    permission_ceiling: str
    allowed_tools: FrozenSet[str]
    blocked_capability_classes: FrozenSet[str]
    reason: str

    def allowed_tool_names(self) -> set[str] | None:
        if not self.allowed_tools:
            return None
        return set(self.allowed_tools)

    def facts(self) -> dict:
        return {
            "name": self.name,
            "permission_ceiling": self.permission_ceiling,
            "allowed_tools": sorted(self.allowed_tools),
            "blocked_capability_classes": sorted(self.blocked_capability_classes),
            "reason": self.reason,
        }


REMOTE_CONNECTOR_TOOL_POLICY = ConnectorToolPolicy(
    name="remote_connector_default",
    permission_ceiling="write",
    allowed_tools=REMOTE_SAFE_TOOLS,
    blocked_capability_classes=REMOTE_BLOCKED_CAPABILITY_CLASSES,
    reason=(
        "Remote connector ingress may prepare tracked work, request approval, inspect status, "
        "and resolve explicit approvals, but it must not expose direct directory, file, shell, browser, "
        "git, test, or destructive watch deletion capabilities."
    ),
)

LOCAL_CONVERSATIONAL_TOOL_POLICY = ConnectorToolPolicy(
    name="local_conversational_default",
    permission_ceiling="write",
    allowed_tools=frozenset(),
    blocked_capability_classes=frozenset(),
    reason="Local conversational loop has full tool access; model decides when to use direct tools vs delegation.",
)


@dataclass(frozen=True)
class ConnectorMessage:
    message_id: str
    peer_id: str
    sender_id: str
    text: str
    source: str
    session_alias_prefix: str

    @property
    def session_alias(self) -> str:
        return f"{self.session_alias_prefix}:{self.peer_id}"

    @property
    def content_key(self) -> str:
        digest = hashlib.md5(self.text.encode()).hexdigest()
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
            disabled_capability_classes=tool_policy.blocked_capability_classes,
            permission_ceiling=tool_policy.permission_ceiling,
            event_bus=self.event_bus,
        )
        self._setup_event_subscriptions()

    def _setup_event_subscriptions(self) -> None:
        from .event_bus import ResponseReadyEvent, UserIntentEvent
        from .governance_agent import GovernanceAgent

        self._governance = GovernanceAgent(self.agent.home, self.event_bus)

        async def on_user_intent(event: UserIntentEvent) -> None:
            result = await self.agent.handle(
                event.text,
                peer_id=event.peer_id,
                sender_id=event.sender_id,
                source=event.source,
                session_alias=event.session_alias,
            )
            await self.event_bus.send_response(ResponseReadyEvent(
                source_agent="main_agent",
                correlation_id=event.correlation_id,
                peer_id=event.peer_id,
                sender_id=event.sender_id,
                text=result.text,
                source=event.source,
            ))

        self.event_bus.subscribe("user_intent", on_user_intent)

    async def handle(self, message: ConnectorMessage) -> str:
        text = await self.router.route(message)
        from .safeguards import redact_secrets
        return redact_secrets(text)
