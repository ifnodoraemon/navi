from __future__ import annotations

import re
import json
from dataclasses import dataclass, field
from typing import Any

from .provider import ChatMessage, ModelPool
from .prompt_os import assemble_planner_system_prompt, assemble_planner_turn_input
from .tools import ToolSpec


@dataclass(frozen=True)
class ModelSyscall:
    tool: str
    permission: str = "read"
    args: dict[str, Any] = field(default_factory=dict)
    model_role: str = "responder"
    message: str = ""
    confidence: float = 0.0
    reason: str = ""


class ModelSyscallPlanner:
    def __init__(self, provider: ModelPool):
        self.provider = provider

    async def plan(
        self,
        text: str,
        *,
        tools: list[ToolSpec],
        conversation_context: str = "",
        observations: list[str] | None = None,
        permission_ceiling: str = "write",
        model_roles: list[str] | None = None,
    ) -> ModelSyscall:
        turn_input = assemble_planner_turn_input(
            text,
            tools=tools,
            conversation_context=conversation_context,
            observations=observations,
            permission_ceiling=permission_ceiling,
            model_roles=model_roles,
        )
        response = await self.provider.complete_for(
            "planner",
            [
                ChatMessage(
                    "system",
                    assemble_planner_system_prompt().render(),
                ),
                ChatMessage("user", turn_input.render()),
            ],
        )
        return self._parse_syscall(response)

    @staticmethod
    def _parse_syscall(response: str) -> ModelSyscall:
        raw = _extract_json_object(response)
        if not raw:
            return ModelSyscall(
                tool="system.planner_error",
                args={"raw_response": response.strip()},
                reason="planner did not return JSON",
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return ModelSyscall(
                tool="system.planner_error",
                args={"raw_response": response.strip()},
                reason="planner returned invalid JSON",
            )
        if not isinstance(data, dict):
            return ModelSyscall(tool="system.planner_error", reason="planner JSON was not an object")
        tool = str(data.get("tool") or "").strip()
        args = _parse_args(data.get("args"))
        message = str(args.get("message") or "")
        return ModelSyscall(
            tool=tool,
            permission=_parse_permission(data.get("permission")),
            args=args,
            model_role=str(data.get("model_role") or "responder").strip() or "responder",
            message=message,
            confidence=_confidence(data.get("confidence")),
            reason=str(data.get("reason") or ""),
        )

def _extract_json_object(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    fenced_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    content = fenced_match.group(1) if fenced_match else text

    start = content.find("{")
    if start == -1:
        return ""

    count = 0
    in_string = False
    escape = False
    for i in range(start, len(content)):
        char = content[i]
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if not in_string:
            if char == "{":
                count += 1
            elif char == "}":
                count -= 1
                if count == 0:
                    return content[start : i + 1]

    start_outer = text.find("{")
    end_outer = text.rfind("}")
    if start_outer >= 0 and end_outer > start_outer:
        return text[start_outer : end_outer + 1]
    return ""


def _confidence(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(parsed, 1.0))


def _parse_permission(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"read", "prepare", "write"}:
        return raw
    return "read"


def _parse_args(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): slot for key, slot in value.items() if slot is not None}
