from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet

from .engine import HernessEngine
from .runtime import AgentRuntime


REMOTE_SAFE_TOOLS = frozenset(
    (
        "final.answer",
        "clarify.ask",
        "delegate.spawn",
        "delegate.prepare",
        "approval.request",
        "delegate.run",
        "watch.create",
        "approval.resolve",
        "delegate.delete",
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
        "filesystem",
        "file",
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

    def allowed_tool_names(self) -> set[str]:
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
        "and resolve explicit approvals, but it must not expose direct filesystem, shell, browser, "
        "git, test, or destructive watch deletion capabilities."
    ),
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
    """Shared IM ingress path into the agent kernel."""

    def __init__(
        self,
        *,
        home: Path,
        runtime: AgentRuntime,
        project_dir: Path,
        allow_sources: set[str] | None = None,
        allowed_tools: set[str] | None = None,
        tool_policy: ConnectorToolPolicy = REMOTE_CONNECTOR_TOOL_POLICY,
    ):
        self.tool_policy = tool_policy
        self.agent = HernessEngine(
            home=home,
            runtime=runtime,
            project_dir=project_dir,
            allow_sources=allow_sources,
            allowed_tools=tool_policy.allowed_tool_names() if allowed_tools is None else allowed_tools,
            permission_ceiling=tool_policy.permission_ceiling,
        )

    async def handle(self, message: ConnectorMessage) -> str:
        result = await self.agent.handle(
            message.text,
            peer_id=message.peer_id,
            sender_id=message.sender_id,
            source=message.source,
            session_alias=message.session_alias,
        )
        return result.text
