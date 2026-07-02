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
        if self.text:
            return self.text
        if self.yields_control:
            if self.facts or self.error_reason:
                return _surface_result_facts(self)
            if self.observation:
                return self.observation
            return _surface_result_facts(self)
        if not self.ok:
            return _surface_result_facts(self)
        return self.text


def _surface_result_facts(result: AgentTurnResult) -> str:
    facts = dict(result.facts or {})
    facts.setdefault("action", result.action)
    facts.setdefault("ok", result.ok)
    if result.error_reason:
        facts.setdefault("error_reason", result.error_reason)
    if result.run_id:
        facts.setdefault("run_id", result.run_id)
    return _surface_facts(facts)


def _surface_facts(facts: dict[str, Any]) -> str:
    lines: list[str] = []
    for key, value in sorted(facts.items()):
        if isinstance(value, (dict, list)):
            import json

            value_text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        else:
            value_text = str(value)
        lines.append(f"{key}={value_text}")
    return "\n".join(lines)
