from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .connector_runtime import ConnectorMessage
from .event_bus import (
    ApprovalCodeEvent,
    EventBus,
    ResponseReadyEvent,
    UserIntentEvent,
)
from .runs import RunStore


_APPROVAL_RE = re.compile(
    r"(?:批准|approve|拒绝|reject)\s*(\d{6})",
    re.IGNORECASE,
)
_BARE_CODE_RE = re.compile(r"^\s*(\d{6})\s*$")


@dataclass(frozen=True)
class Classification:
    kind: str  # "approval_code" | "user_intent"
    code: str = ""
    decision: str = ""


class ApprovalCodeDetector:
    def __init__(self, home: Path) -> None:
        self.runs = RunStore(home)

    def classify(self, text: str) -> Classification:
        m = _APPROVAL_RE.search(text)
        if m:
            code = m.group(1)
            if self.runs.get_approval(code):
                decision = "reject" if any(w in text.lower() for w in ("拒绝", "reject")) else "approve"
                return Classification(kind="approval_code", code=code, decision=decision)

        m = _BARE_CODE_RE.match(text)
        if m:
            code = m.group(1)
            if self.runs.get_approval(code):
                return Classification(kind="approval_code", code=code, decision="approve")

        return Classification(kind="user_intent")


class ConnectorRouter:
    def __init__(self, home: Path, event_bus: EventBus) -> None:
        self.home = home
        self.event_bus = event_bus
        self.detector = ApprovalCodeDetector(home)

    async def route(self, message: ConnectorMessage) -> str:
        correlation_id = message.message_id
        channel = self.event_bus.create_response_channel(correlation_id)

        try:
            classification = self.detector.classify(message.text)

            if classification.kind == "approval_code":
                event = ApprovalCodeEvent(
                    source_agent="connector_router",
                    correlation_id=correlation_id,
                    message_id=message.message_id,
                    peer_id=message.peer_id,
                    sender_id=message.sender_id,
                    code=classification.code,
                    decision=classification.decision,
                    source=message.source,
                )
            else:
                event = UserIntentEvent(
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
