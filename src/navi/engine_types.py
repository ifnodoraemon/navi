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
    approval_affordance: str = ""

    def surfaced_text(self) -> str:
        """The text to surface to the user.

        The model's ``text`` is preserved verbatim. The runtime approval
        affordance (when the model did not already surface the approval
        code) is appended as a separate trailing block rather than
        rewritten into the model's utterance.
        """
        if not self.approval_affordance:
            return self.text
        text = self.text.strip()
        if text:
            return f"{text}\n\n{self.approval_affordance}"
        return self.approval_affordance
