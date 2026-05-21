from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .capabilities import CapabilityContext, CapabilityRegistry
from .config import load_config
from .operating_context import OperatingContext
from .provider import ChatMessage
from .runtime import AgentRuntime
from .syscalls import ModelSyscallPlanner


@dataclass(frozen=True)
class AgentTurnResult:
    text: str
    session_id: str = ""
    task_id: str = ""
    action: str = "chat"
    observation: str = ""
    model_role: str = "responder"
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
        step_budget: int | None = None,
    ):
        self.home = home
        self.runtime = runtime
        self.permission_ceiling = permission_ceiling
        self.step_budget = step_budget or load_config(home).runtime.agent_step_budget
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
        for _ in range(self.step_budget):
            syscall = await self.planner.plan(
                text,
                tools=self.capabilities.planner_specs(permission_ceiling=context.permission_ceiling),
                conversation_context=self._conversation_context(resolved_session_id),
                observations=observations,
                permission_ceiling=context.permission_ceiling,
                model_roles=self.runtime.model_roles(),
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
                model_role=syscall.model_role,
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
                    model_role=last_result.model_role,
                    terminal=True,
                )
            last_result = result
            if result.terminal:
                turn_res = self._record_turn(text, result, session_id=resolved_session_id)
                self._trigger_background_memory(turn_res)
                return turn_res
            observations.append(result.observation or result.text)

        if observations:
            turn_res = await self._finalize_observations(
                text,
                observations,
                session_id=resolved_session_id,
                action=last_result.action if last_result else "capability",
                task_id=last_result.task_id if last_result else "",
                model_role=last_result.model_role if last_result else "responder",
            )
            self._trigger_background_memory(turn_res)
            return turn_res
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
        turn_res = AgentTurnResult(text=reply.content, session_id=reply.session_id, action="chat", terminal=True)
        self._trigger_background_memory(turn_res)
        return turn_res

    def _trigger_background_memory(self, result: AgentTurnResult) -> None:
        import asyncio
        if result.session_id:
            asyncio.create_task(
                self.runtime.memory.extract_and_consolidate_memories(
                    session_id=result.session_id,
                    provider=self.runtime.provider,
                    task_id=result.task_id,
                )
            )


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
            model_role=result.model_role,
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
        model_role: str = "responder",
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
        answer = await self.runtime.complete(messages, role=model_role)
        self.runtime.memory.add_message(session_id, "assistant", answer)
        return AgentTurnResult(
            text=answer,
            session_id=session_id,
            task_id=task_id,
            action=action,
            observation=observation,
            model_role=model_role,
            terminal=True,
        )
