from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .memory import MemoryStore
from .operating_context import OperatingContext
from .prompting import build_system_prompt
from .provider import ChatMessage, ModelPool
from .skills import SkillStore


@dataclass(frozen=True)
class AssistantReply:
    session_id: str
    content: str


class AgentRuntime:
    def __init__(self, *, home: Path, provider: ModelPool):
        self.home = home
        self.provider = provider
        self.memory = MemoryStore(home)
        self.skills = SkillStore(home)

    async def chat(
        self,
        user_text: str,
        session_id: str | None = None,
        operating_context: OperatingContext | None = None,
    ) -> AssistantReply:
        session_id = session_id or self.memory.new_session_id()
        self.memory.add_message(session_id, "user", user_text)

        messages = self.build_messages(
            session_id, user_text=user_text, operating_context=operating_context
        )
        answer = await self.complete(messages, role="responder")
        self.memory.add_message(session_id, "assistant", answer)
        return AssistantReply(session_id=session_id, content=answer)

    async def complete(self, messages: list[ChatMessage], *, role: str = "default") -> str:
        return await self.provider.complete_for(role, messages)

    def usage_for(self, role: str) -> dict:
        return self.provider.usage_for(role)

    def build_messages(
        self,
        session_id: str,
        *,
        user_text: str = "",
        operating_context: OperatingContext | None = None,
    ) -> list[ChatMessage]:
        operating_context = operating_context or OperatingContext(home=self.home)
        from .memory.scopes import memory_scopes_for_context

        memory_context = self.memory.render_context(
            user_text,
            goal=operating_context.objective or "",
            allowed_scopes=set(
                memory_scopes_for_context(
                    source=operating_context.source,
                    peer_id=operating_context.peer_id,
                    sender_id=operating_context.sender_id,
                    session_id=session_id,
                    workspace=operating_context.workspace,
                )
            ),
        )
        skills_context = self.skills.render_prompt(
            permission_ceiling=operating_context.skill_permission_ceiling,
            workspace=operating_context.workspace,
            role=operating_context.role,
        )

        system_prompt = build_system_prompt(
            home=self.home,
            memory_context=memory_context,
            skills_context=skills_context,
            operating_context=operating_context,
        )
        messages = [
            ChatMessage(
                "system",
                system_prompt,
            )
        ]
        for item in self.memory.get_messages(session_id):
            messages.append(ChatMessage(item.role, item.content))
        return messages
