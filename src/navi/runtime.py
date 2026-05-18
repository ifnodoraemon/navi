from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .memory import MemoryStore
from .prompting import PromptContext, build_system_prompt
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

    async def chat(
        self,
        user_text: str,
        session_id: str | None = None,
        *,
        prompt_context: PromptContext | None = None,
    ) -> AssistantReply:
        session_id = session_id or self.memory.new_session_id()
        self.memory.add_message(session_id, "user", user_text)

        messages = self._build_messages(session_id, user_text=user_text, prompt_context=prompt_context)
        answer = await self.provider.complete(messages)
        self.memory.add_message(session_id, "assistant", answer)
        return AssistantReply(session_id=session_id, content=answer)

    def _build_messages(
        self,
        session_id: str,
        *,
        user_text: str = "",
        prompt_context: PromptContext | None = None,
    ) -> list[ChatMessage]:
        memory_context = self.memory.render_context(user_text)
        skills_context = self.skills.render_prompt()

        messages = [
            ChatMessage(
                "system",
                build_system_prompt(
                    home=self.home,
                    memory_context=memory_context,
                    skills_context=skills_context,
                    prompt_context=prompt_context,
                ),
            )
        ]
        for item in self.memory.get_messages(session_id):
            messages.append(ChatMessage(item.role, item.content))
        return messages
