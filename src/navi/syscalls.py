from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .provider import ChatMessage, ModelPool
from .spec_loader import load_spec
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
        model_roles = model_roles or ["default", "planner", "responder", "notification"]
        user_parts = []
        if conversation_context.strip():
            user_parts.extend((
                "Recent conversation:",
                "<conversation_history>",
                conversation_context.strip(),
                "</conversation_history>",
            ))
        if observations:
            user_parts.extend((
                "Observed facts in this turn:",
                "<observed_facts>",
                "\n\n".join(observations),
                "</observed_facts>",
            ))
        user_parts.extend(
            (
                "Current user message:",
                "<user_message>",
                text,
                "</user_message>",
                f"Permission ceiling: {permission_ceiling}",
                "Available model roles:",
                json.dumps(model_roles, ensure_ascii=False),
                "Available tools:",
                json.dumps([asdict(tool) for tool in tools], ensure_ascii=False),
            )
        )
        response = await self.provider.complete_for(
            "planner",
            [
                ChatMessage(
                    "system",
                    _planner_system_prompt(),
                ),
                ChatMessage("user", "\n".join(user_parts)),
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


def _planner_system_prompt() -> str:
    spec = load_spec("syscall_planner.yaml") or {}
    lines = [str(line) for line in spec.get("system_lines") or []]
    routing_rules = [str(rule) for rule in spec.get("routing_rules") or []]
    if routing_rules:
        lines.append("[TASK ROUTING RULES:")
        lines.extend(f"{idx}. {rule}" for idx, rule in enumerate(routing_rules, start=1))
        lines[-1] = f"{lines[-1]}]"
    security_guidelines = [str(rule) for rule in spec.get("security_guidelines") or []]
    if security_guidelines:
        lines.append("[SECURITY GUIDELINE:")
        lines.extend(f"- {rule}" for rule in security_guidelines)
        lines[-1] = f"{lines[-1]}]"
    return "\n".join(lines)


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
