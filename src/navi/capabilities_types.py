from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .tools import ToolSpec


@dataclass(frozen=True)
class CapabilityContext:
    home: Path
    peer_id: str = ""
    sender_id: str = ""
    source: str = "local"
    permission_ceiling: str = "write"
    workspace: str = ""
    session_id: str | None = None
    input_text: str = ""
    event_bus: Any | None = None


@dataclass(frozen=True)
class CapabilityResult:
    ok: bool
    action: str
    observation: str
    message: str = ""
    run_id: str = ""
    terminal: bool = False
    facts: dict[str, Any] | None = None
    provenance: str = ""
    error_reason: str = "unknown"


@dataclass(frozen=True)
class CapabilityNode:
    name: str
    source: str
    permission: str
    facts_only: bool
    mutates: bool
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    provider: str
    description: str = ""


class Capability(Protocol):
    spec: ToolSpec

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult: ...


class CapabilityProvider(Protocol):
    def capabilities(self) -> Mapping[str, Capability]: ...
