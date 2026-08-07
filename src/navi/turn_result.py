"""Turn result types for the Navi control plane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any



@dataclass(frozen=True)
class AgentTurnResult:
    text: str
    session_id: str = ""
    run_id: str = ""
    action: str = "chat"
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
        return self.text
