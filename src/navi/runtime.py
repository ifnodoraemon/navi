from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .memory import MemoryStore
from .provider import ChatMessage, ChatProvider
from .skills import SkillStore


@dataclass(frozen=True)
class AssistantReply:
    session_id: str
    content: str


class AgentRuntime:
    def __init__(self, *, home: Path, provider: ChatProvider):
        self.home = home
        self.provider = provider
        self.memory = MemoryStore(home)
        self.skills = SkillStore(home)

    async def chat(self, user_text: str, session_id: str | None = None) -> AssistantReply:
        session_id = session_id or self.memory.new_session_id()
        self.memory.add_message(session_id, "user", user_text)

        messages = self._build_messages(session_id)
        answer = await self.provider.complete(messages)
        self.memory.add_message(session_id, "assistant", answer)
        return AssistantReply(session_id=session_id, content=answer)

    def _build_messages(self, session_id: str) -> list[ChatMessage]:
        system_parts = [
            "You are Navi, a local-first personal AI assistant.",
            "Be concise, practical, and preserve user privacy.",
        ]
        memory_context = self.memory.read_memory()
        if memory_context:
            system_parts.append(f"Persistent memory:\n{memory_context}")
        skills_context = self.skills.render_prompt()
        if skills_context:
            system_parts.append(f"Installed skills:\n{skills_context}")

        messages = [ChatMessage("system", "\n\n".join(system_parts))]
        for item in self.memory.get_messages(session_id):
            messages.append(ChatMessage(item.role, item.content))
        return messages
