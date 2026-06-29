"""Type definitions for the HernessEngine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .conversation_contract import CONVERSATION_ACTION_CHAT


@dataclass(frozen=True)
class AgentTurnResult:
    text: str
    session_id: str = ""
    run_id: str = ""
    action: str = CONVERSATION_ACTION_CHAT
    observation: str = ""
    model_role: str = "responder"
    terminal: bool = False
    trace_id: str = ""
    memory_influence: tuple[str, ...] = ()
    facts: dict[str, Any] | None = None

    ok: bool = True
    yields_control: bool = False
    error_reason: str = ""

    def surfaced_text(self) -> str:
        """The text to surface to the user."""
        if self.text:
            return self.text
        if not self.ok:
            error_msg = ""
            if self.facts:
                error_msg = self.facts.get("error") or self.facts.get("error_type") or self.error_reason
            else:
                error_msg = self.error_reason
            return f"[{self.action}] failed: {error_msg}"
        return self.text
