from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .capabilities import CapabilityContext, CapabilityRegistry
from .operating_context import OperatingContext
from .syscalls import ModelSyscallPlanner
from .provider import ChatMessage
from .runtime import AgentRuntime

MAX_AGENT_STEPS = 4


@dataclass(frozen=True)
class AgentTurnResult:
    text: str
    session_id: str = ""
    task_id: str = ""
    action: str = "chat"
    observation: str = ""
    terminal: bool = False


class HernessEngine:
    """Model-owned observe/plan/syscall/observe loop."""

    def __init__(
        self,
        *,
        home: Path,
        runtime: AgentRuntime,
        project_dir: Path | None = None,
        allow_sources: set[str] | None = None,
        disabled_tools: set[str] | None = None,
        permission_ceiling: str = "write",
    ):
        self.home = home
        self.runtime = runtime
        self.permission_ceiling = permission_ceiling
        self.capabilities = CapabilityRegistry(
            home=home,
            project_dir=project_dir or Path.cwd(),
            allow_sources=allow_sources,
            disabled_tools=disabled_tools,
            permission_ceiling=permission_ceiling,
        )
        self.planner = ModelSyscallPlanner(runtime.provider)

    async def handle(
        self,
        text: str,
        *,
        peer_id: str,
        sender_id: str,
        source: str,
        session_id: str | None = None,
        session_alias: str | None = None,
    ) -> AgentTurnResult:
        resolved_session_id = session_id
        if not resolved_session_id and session_alias:
            resolved_session_id = self.runtime.memory.current_session_id(session_alias)

        context = CapabilityContext(
            home=self.home,
            peer_id=peer_id,
            sender_id=sender_id,
            source=source,
            permission_ceiling=self.permission_ceiling,
        )
        observations: list[str] = []
        last_result: AgentTurnResult | None = None
        for _ in range(MAX_AGENT_STEPS):
            syscall = await self.planner.plan(
                text,
                tools=self.capabilities.planner_specs(permission_ceiling=context.permission_ceiling),
                conversation_context=self._conversation_context(resolved_session_id),
                observations=observations,
                permission_ceiling=context.permission_ceiling,
            )
            invoked = await self.capabilities.invoke(
                syscall.tool,
                syscall.args,
                permission=syscall.permission,
                context=context,
            )
            result = AgentTurnResult(
                text=invoked.message or invoked.observation,
                task_id=invoked.task_id,
                action=invoked.action,
                observation=invoked.observation,
                terminal=invoked.terminal,
            )
            if result.terminal and result.action == "chat" and not result.text.strip():
                break
            if result.terminal and observations and result.action == "chat" and last_result:
                result = AgentTurnResult(
                    text=result.text,
                    task_id=last_result.task_id,
                    action=last_result.action,
                    observation="\n\n".join(observations),
                    terminal=True,
                )
            last_result = result
            if result.terminal:
                return self._record_turn(text, result, session_id=resolved_session_id)
            observations.append(result.observation or result.text)

        if observations:
            return await self._finalize_observations(
                text,
                observations,
                session_id=resolved_session_id,
                action=last_result.action if last_result else "capability",
                task_id=last_result.task_id if last_result else "",
            )
        reply = await self.runtime.chat(
            text,
            session_id=resolved_session_id,
            operating_context=OperatingContext(
                home=self.home,
                source=source,
                peer_id=peer_id,
                sender_id=sender_id,
                permission_ceiling=self.permission_ceiling,
                skill_permission_ceiling="read",
            ),
        )
        return AgentTurnResult(text=reply.content, session_id=reply.session_id, action="chat", terminal=True)

    def _conversation_context(self, session_id: str | None) -> str:
        if not session_id:
            return ""
        messages = self.runtime.memory.get_messages(session_id, limit=8)
        return "\n".join(f"{item.role}: {item.content}" for item in messages)

    def _record_turn(
        self,
        user_text: str,
        result: AgentTurnResult,
        *,
        session_id: str | None,
    ) -> AgentTurnResult:
        session_id = session_id or self.runtime.memory.new_session_id()
        self.runtime.memory.add_message(session_id, "user", user_text)
        self.runtime.memory.add_message(session_id, "assistant", result.text)
        return AgentTurnResult(
            text=result.text,
            session_id=session_id,
            task_id=result.task_id,
            action=result.action,
            observation=result.observation,
            terminal=result.terminal,
        )

    async def _finalize_observations(
        self,
        user_text: str,
        observations: list[str],
        *,
        session_id: str | None,
        action: str,
        task_id: str = "",
    ) -> AgentTurnResult:
        session_id = session_id or self.runtime.memory.new_session_id()
        observation = "\n\n".join(observations)
        self.runtime.memory.add_message(session_id, "user", user_text)
        messages = self.runtime._build_messages(
            session_id,
            user_text=user_text,
            operating_context=OperatingContext(
                home=self.home,
                permission_ceiling=self.permission_ceiling,
                skill_permission_ceiling="read",
            ),
        )
        messages.append(
            ChatMessage(
                "system",
                "\n".join(
                    (
                        "Navi's operating system has produced capability observations.",
                        "Use only the observations as the source of truth.",
                        "Speak directly to the user in the user's language.",
                        "Do not mention hidden routers, JSON schemas, or internal planning decisions.",
                        "Preserve task ids, approval codes, service names, and error messages exactly when relevant.",
                    )
                ),
            )
        )
        messages.append(
            ChatMessage(
                "user",
                "\n".join(
                    (
                        f"User request: {user_text}",
                        "Capability observations:",
                        observation,
                    )
                ),
            )
        )
        answer = await self.runtime.provider.complete(messages)
        self.runtime.memory.add_message(session_id, "assistant", answer)
        return AgentTurnResult(
            text=answer,
            session_id=session_id,
            task_id=task_id,
            action=action,
            observation=observation,
            terminal=True,
        )


AgentKernel = HernessEngine
