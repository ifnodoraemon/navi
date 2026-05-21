from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .agent_kernel import AgentKernel
from .runtime import AgentRuntime


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
        allow_sources: set[str] | None = None,
    ):
        self.agent = AgentKernel(
            home=home,
            runtime=runtime,
            project_dir=Path.cwd(),
            allow_sources=allow_sources,
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
