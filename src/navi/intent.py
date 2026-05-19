from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .provider import ChatMessage, ChatProvider
from .tools import ToolSpec


@dataclass(frozen=True)
class ActionDecision:
    kind: str
    message: str = ""
    prompt: str = ""
    cron: str = ""
    target_id: str = ""
    confidence: float = 0.0
    reason: str = ""


class AgenticActionSelector:
    def __init__(self, provider: ChatProvider):
        self.provider = provider

    async def select(self, text: str, *, tools: list[ToolSpec]) -> ActionDecision:
        response = await self.provider.complete(
            [
                ChatMessage(
                    "system",
                    "\n".join(
                        (
                            "You are Navi's action selector.",
                            "Return only one JSON object. Do not answer the user directly.",
                            "Available actions:",
                            "- chat: ordinary conversation, no local action.",
                            "- ask: ask one concise clarification question.",
                            "- task: create a tracked local task when the user asks Navi to do local work.",
                            "- watch: create a recurring watch only when an exact cron expression can be derived from the user text.",
                            "- service_status: call a service status fact tool.",
                            "- task_status: call a task status fact tool.",
                            "Rules:",
                            "- Use the available capabilities; do not tell the user to type slash commands.",
                            "- If a recurring request has a vague time such as morning/evening without an exact hour, choose ask.",
                            "- Do not invent default times, paths, services, ids, permissions, or credentials.",
                            "- Tools return facts only; policy and next action stay outside tool results.",
                            "JSON schema:",
                            '{"kind":"chat|ask|task|watch|service_status|task_status","message":"","prompt":"","cron":"","target_id":"","confidence":0.0,"reason":""}',
                        )
                    ),
                ),
                ChatMessage(
                    "user",
                    "\n".join(
                        (
                            f"User message: {text}",
                            "Available fact tools:",
                            json.dumps([tool.name for tool in tools], ensure_ascii=False),
                        )
                    ),
                ),
            ]
        )
        return self._parse_decision(response)

    @staticmethod
    def _parse_decision(response: str) -> ActionDecision:
        raw = _extract_json_object(response)
        if not raw:
            return ActionDecision(kind="chat", reason="selector did not return JSON")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return ActionDecision(kind="chat", reason="selector returned invalid JSON")
        if not isinstance(data, dict):
            return ActionDecision(kind="chat", reason="selector JSON was not an object")
        kind = str(data.get("kind") or "chat")
        if kind not in {"chat", "ask", "task", "watch", "service_status", "task_status"}:
            kind = "chat"
        return ActionDecision(
            kind=kind,
            message=str(data.get("message") or ""),
            prompt=str(data.get("prompt") or ""),
            cron=str(data.get("cron") or ""),
            target_id=str(data.get("target_id") or ""),
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
