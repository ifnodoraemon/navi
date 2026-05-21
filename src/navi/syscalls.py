from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .provider import ChatMessage, ChatProvider, complete_with_role
from .tools import ToolSpec


@dataclass(frozen=True)
class ModelSyscall:
    tool: str
    permission: str = "read"
    args: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    confidence: float = 0.0
    reason: str = ""


class ModelSyscallPlanner:
    def __init__(self, provider: ChatProvider):
        self.provider = provider

    async def plan(
        self,
        text: str,
        *,
        tools: list[ToolSpec],
        conversation_context: str = "",
        observations: list[str] | None = None,
        permission_ceiling: str = "write",
    ) -> ModelSyscall:
        user_parts = []
        if conversation_context.strip():
            user_parts.extend(("Recent conversation:", conversation_context.strip()))
        if observations:
            user_parts.extend(("Observed facts in this turn:", "\n\n".join(observations)))
        user_parts.extend(
            (
                f"Current user message: {text}",
                f"Permission ceiling: {permission_ceiling}",
                "Available tools:",
                json.dumps([asdict(tool) for tool in tools], ensure_ascii=False),
            )
        )
        response = await complete_with_role(
            self.provider,
            "planner",
            [
                ChatMessage(
                    "system",
                    "\n".join(
                        (
                            "You are Navi's model syscall planner.",
                            "Navi is an agent operating system. Select the next syscall from the capability manifest.",
                            "Return exactly one JSON object and no prose.",
                            "The capability manifest is authoritative for names, permissions, schemas, and effects.",
                            "Never request a permission above the permission ceiling.",
                            "Use recent conversation and observations as state. Decide the next syscall yourself.",
                            "If no syscall should run, select an answer/clarification capability from the manifest.",
                            "JSON shape:",
                            '{"tool":"<available_tool_name>","permission":"read|prepare|write","args":{},"confidence":0.0,"reason":""}',
                        )
                    ),
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
        message = str(data.get("message") or "")
        if message and not args.get("message"):
            args = {**args, "message": message}
        return ModelSyscall(
            tool=tool,
            permission=_parse_permission(data.get("permission")),
            args=args,
            message=message or str(args.get("message") or ""),
            confidence=_confidence(data.get("confidence")),
            reason=str(data.get("reason") or ""),
        )


def _extract_json_object(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
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
