from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .capabilities import CapabilityContext, CapabilityRegistry
from .config import load_config
from .connector_registry import approval_surface_affordance
from .goals import GoalStore
from .operating_context import OperatingContext
from .provider import ChatMessage
from .recovery import RecoveryPlanner
from .runtime import AgentRuntime
from .syscalls import ModelSyscallPlanner
from .trace import TraceStore


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


class HernessEngine:
    """Model-owned observe/plan/syscall/observe loop."""

    def __init__(
        self,
        *,
        home: Path,
        runtime: AgentRuntime,
        project_dir: Path,
        allow_sources: set[str] | None = None,
        allowed_tools: set[str] | None = None,
        disabled_tools: set[str] | None = None,
        permission_ceiling: str = "write",
        step_budget: int | None = None,
    ):
        self.home = home
        self.runtime = runtime
        self.permission_ceiling = permission_ceiling
        self.step_budget = step_budget if step_budget is not None else load_config(home).runtime.agent_step_budget
        self.capabilities = CapabilityRegistry(
            home=home,
            project_dir=project_dir,
            allow_sources=allow_sources,
            allowed_tools=allowed_tools,
            disabled_tools=disabled_tools,
            permission_ceiling=permission_ceiling,
        )
        self.planner = ModelSyscallPlanner(runtime.provider)
        self.recovery = RecoveryPlanner()
        self.trace = TraceStore(home)
        self._memory_sem: asyncio.Semaphore | None = None
        self._background_tasks: set[asyncio.Task] = set()

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
        trace_id = self.trace.new_trace_id()
        self.trace.add_event(
            trace_id=trace_id,
            phase="turn.start",
            session_id=resolved_session_id or "",
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            input_data={"message": text, "session_alias": session_alias or ""},
        )

        context = CapabilityContext(
            home=self.home,
            peer_id=peer_id,
            sender_id=sender_id,
            source=source,
            permission_ceiling=self.permission_ceiling,
            workspace=str(self.capabilities.gateway.project_dir.resolve()),
        )
        observations: list[str] = []
        completion_events: list[dict[str, Any]] = []
        goal_ids: set[str] = set()
        pending_approval_prompt = ""
        last_result: AgentTurnResult | None = None
        budget_exhausted = False
        for _ in range(self.step_budget):
            syscall = await self.planner.plan(
                text,
                tools=self.capabilities.planner_specs(permission_ceiling=context.permission_ceiling),
                conversation_context=self._conversation_context(resolved_session_id),
                observations=observations,
                permission_ceiling=context.permission_ceiling,
                model_roles=self.runtime.model_roles(),
            )
            self.trace.add_event(
                trace_id=trace_id,
                phase="planner.syscall",
                session_id=resolved_session_id or "",
                source=source,
                peer_id=peer_id,
                sender_id=sender_id,
                tool=syscall.tool,
                model_role="planner",
                ok=syscall.tool != "system.planner_error",
                input_data={"observations_count": len(observations), "permission_ceiling": context.permission_ceiling},
                output_data=asdict(syscall),
                message=syscall.reason,
            )
            invoked = await self.capabilities.invoke(
                syscall.tool,
                syscall.args,
                permission=syscall.permission,
                context=context,
            )
            completion_events.append(
                {
                    "tool": syscall.tool,
                    "ok": invoked.ok,
                    "facts": invoked.facts or {},
                    "action": invoked.action,
                }
            )
            goal_id = str((invoked.facts or {}).get("goal_id") or "").strip()
            if goal_id:
                goal_ids.add(goal_id)
            self.trace.add_event(
                trace_id=trace_id,
                phase="capability.result",
                session_id=resolved_session_id or "",
                run_id=invoked.run_id,
                source=source,
                peer_id=peer_id,
                sender_id=sender_id,
                tool=syscall.tool,
                model_role=syscall.model_role,
                ok=invoked.ok,
                input_data={"args": syscall.args, "permission": syscall.permission},
                output_data={"action": invoked.action, "facts": invoked.facts or {}, "terminal": invoked.terminal},
                message=invoked.message or invoked.observation,
            )
            approval_prompt = self._approval_prompt_from_facts(invoked.facts, source=source)
            if approval_prompt:
                pending_approval_prompt = approval_prompt
            result = AgentTurnResult(
                text=invoked.message or invoked.observation,
                run_id=invoked.run_id,
                action=invoked.action,
                observation=invoked.observation,
                model_role=syscall.model_role,
                terminal=invoked.terminal,
            )
            if result.terminal and observations and result.action == "chat" and last_result:
                result = AgentTurnResult(
                    text=result.text,
                    run_id=last_result.run_id,
                    action=last_result.action,
                    observation="\n\n".join(observations),
                    model_role=last_result.model_role,
                    terminal=True,
                )
                result = self._ensure_pending_approval_prompt(result, pending_approval_prompt)
            last_result = result
            if result.terminal:
                block_reason = self._completion_block_reason(completion_events)
                if block_reason:
                    recovery_plan = self.recovery.plan_completion_failure(
                        block_reason=block_reason,
                        events=completion_events,
                    )
                    observations.append(recovery_plan.to_observation())
                    self.trace.add_event(
                        trace_id=trace_id,
                        phase="completion.verify",
                        session_id=resolved_session_id or "",
                        run_id=result.run_id,
                        source=source,
                        peer_id=peer_id,
                        sender_id=sender_id,
                        tool=syscall.tool,
                        model_role=syscall.model_role,
                        ok=False,
                        output_data={"reason": block_reason},
                        message=block_reason,
                    )
                    self.trace.add_event(
                        trace_id=trace_id,
                        phase="recovery.plan",
                        session_id=resolved_session_id or "",
                        run_id=result.run_id,
                        source=source,
                        peer_id=peer_id,
                        sender_id=sender_id,
                        tool=syscall.tool,
                        model_role="planner",
                        ok=True,
                        input_data={"block_reason": block_reason},
                        output_data=asdict(recovery_plan),
                        message=recovery_plan.recommended,
                    )
                    continue
                turn_res = self._record_turn(text, result, session_id=resolved_session_id)
                turn_res = self._with_trace(turn_res, trace_id)
                self._attach_goals(goal_ids, trace_id=trace_id, session_id=turn_res.session_id, evidence={"final_action": turn_res.action})
                self._record_trace_final(turn_res, trace_id, source=source, peer_id=peer_id, sender_id=sender_id)
                self._trigger_background_memory(turn_res)
                return turn_res
            observations.append(result.observation or result.text)
        else:
            budget_exhausted = True

        if observations:
            turn_res = await self._finalize_observations(
                text,
                observations,
                session_id=resolved_session_id,
                trace_id=trace_id,
                source=source,
                peer_id=peer_id,
                sender_id=sender_id,
                action=last_result.action if last_result else "capability",
                run_id=last_result.run_id if last_result else "",
                model_role=last_result.model_role if last_result else "responder",
                pending_approval_prompt=pending_approval_prompt,
                budget_exhausted=budget_exhausted,
            )
            turn_res = self._with_trace(turn_res, trace_id)
            self._attach_goals(goal_ids, trace_id=trace_id, session_id=turn_res.session_id, evidence={"final_action": turn_res.action, "budget_exhausted": budget_exhausted})
            self._record_trace_final(turn_res, trace_id, source=source, peer_id=peer_id, sender_id=sender_id)
            self._trigger_background_memory(turn_res)
            return turn_res

        if budget_exhausted:
            resolved_session_id = resolved_session_id or self.runtime.memory.new_session_id()
            self.runtime.memory.add_message(resolved_session_id, "user", text)
            messages = self.runtime._build_messages(
                resolved_session_id,
                user_text=text,
                operating_context=OperatingContext(
                    home=self.home,
                    source=source,
                    peer_id=peer_id,
                    sender_id=sender_id,
                    permission_ceiling=self.permission_ceiling,
                    skill_permission_ceiling="read",
                    workspace=str(self.capabilities.gateway.project_dir.resolve()),
                ),
            )
            answer = await self.runtime.complete(messages, role="responder")
            warning = "\n\n(注意：已达到步骤预算上限，任务可能未完成。) / (Warning: Step budget limit reached, the task may not be completed.)"
            answer_with_warning = f"{answer}{warning}"
            self.runtime.memory.add_message(resolved_session_id, "assistant", answer_with_warning)
            turn_res = AgentTurnResult(text=answer_with_warning, session_id=resolved_session_id, action="chat", terminal=True, trace_id=trace_id)
            self._record_trace_final(turn_res, trace_id, source=source, peer_id=peer_id, sender_id=sender_id)
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
                workspace=str(self.capabilities.gateway.project_dir.resolve()),
            ),
        )
        turn_res = AgentTurnResult(text=reply.content, session_id=reply.session_id, action="chat", terminal=True, trace_id=trace_id)
        self._record_trace_final(turn_res, trace_id, source=source, peer_id=peer_id, sender_id=sender_id)
        self._trigger_background_memory(turn_res)
        return turn_res

    def _attach_goals(self, goal_ids: set[str], *, trace_id: str, session_id: str, evidence: dict[str, Any]) -> None:
        if not goal_ids:
            return
        goals = GoalStore(self.home)
        for goal_id in sorted(goal_ids):
            goals.attach_trace(goal_id, trace_id=trace_id, session_id=session_id, evidence=evidence)

    @staticmethod
    def _completion_block_reason(events: list[dict[str, Any]]) -> str:
        if not events:
            return ""
        latest_run_status: dict[str, str] = {}
        for event in events:
            facts = event.get("facts")
            if not isinstance(facts, dict):
                continue
            run_id = str(facts.get("run_id") or facts.get("run_id") or "").strip()
            status = str(facts.get("status") or facts.get("run_status") or facts.get("run_status") or "").strip()
            if run_id and status:
                latest_run_status[run_id] = status
        for event in events:
            if event.get("tool") != "delegate.spawn":
                continue
            facts = event.get("facts")
            if not isinstance(facts, dict):
                continue
            run_id = str(facts.get("run_id") or facts.get("run_id") or "").strip()
            status = latest_run_status.get(run_id) or str(facts.get("status") or "").strip()
            if run_id and status in {"pending", "prepared"}:
                return (
                    "completion verifier blocked final answer: "
                    f"delegation run {run_id} is still {status}; prepare it and request approval or run it before reporting completion."
                )
        last_delete = next((event for event in reversed(events) if event.get("tool") == "delegate.delete"), None)
        facts = last_delete.get("facts") if isinstance(last_delete, dict) else None
        if isinstance(facts, dict) and facts.get("cleanup_complete") is False:
            remaining = facts.get("remaining_count")
            return (
                "completion verifier blocked final answer: "
                f"delegate.delete left {remaining} failed delegation runs; continue cleanup or report the remaining count explicitly."
            )
        return ""

    def _trigger_background_memory(self, result: AgentTurnResult) -> None:
        if result.session_id:
            logger = logging.getLogger("navi.engine")

            async def run_with_semaphore():
                async with self._memory_semaphore():
                    await self.runtime.memory.extract_and_consolidate_memories(
                        session_id=result.session_id,
                        provider=self.runtime.provider,
                        run_id=result.run_id,
                    )

            task = asyncio.create_task(run_with_semaphore())
            self._background_tasks.add(task)
            def handle_done(t: asyncio.Run) -> None:
                self._background_tasks.discard(t)
                try:
                    t.result()
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"Background memory extraction failed: {e}", exc_info=True)
            task.add_done_callback(handle_done)

    async def shutdown(self, *, timeout: float = 10.0) -> None:
        if not self._background_tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tuple(self._background_tasks), return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            for task in list(self._background_tasks):
                task.cancel()
            await asyncio.gather(*tuple(self._background_tasks), return_exceptions=True)

    def _memory_semaphore(self) -> asyncio.Semaphore:
        if self._memory_sem is None:
            self._memory_sem = asyncio.Semaphore(2)
        return self._memory_sem

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
            run_id=result.run_id,
            action=result.action,
            observation=result.observation,
            model_role=result.model_role,
            terminal=result.terminal,
            trace_id=result.trace_id,
        )

    @staticmethod
    def _with_trace(result: AgentTurnResult, trace_id: str) -> AgentTurnResult:
        return AgentTurnResult(
            text=result.text,
            session_id=result.session_id,
            run_id=result.run_id,
            action=result.action,
            observation=result.observation,
            model_role=result.model_role,
            terminal=result.terminal,
            trace_id=trace_id,
        )

    def _record_trace_final(
        self,
        result: AgentTurnResult,
        trace_id: str,
        *,
        source: str,
        peer_id: str,
        sender_id: str,
    ) -> None:
        self.trace.add_event(
            trace_id=trace_id,
            phase="turn.final",
            session_id=result.session_id,
            run_id=result.run_id,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            model_role=result.model_role,
            ok=True,
            output_data={"action": result.action, "terminal": result.terminal},
            message=result.text,
        )
        self.trace.evaluate_trace(trace_id)

    async def _finalize_observations(
        self,
        user_text: str,
        observations: list[str],
        *,
        session_id: str | None,
        trace_id: str,
        source: str,
        peer_id: str,
        sender_id: str,
        action: str,
        run_id: str = "",
        model_role: str = "responder",
        pending_approval_prompt: str = "",
        budget_exhausted: bool = False,
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
                workspace=str(self.capabilities.gateway.project_dir.resolve()),
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
        self.trace.add_event(
            trace_id=trace_id,
            phase="agent.role_result",
            session_id=session_id,
            run_id=run_id,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            model_role=model_role,
            ok=True,
            input_data={"observations_count": len(observations), "action": action},
            output_data={"response_chars": len(answer), "budget_exhausted": budget_exhausted},
            message=f"{model_role} synthesized response",
        )
        if pending_approval_prompt and not self._text_mentions_pending_approval(
            answer,
            pending_approval_prompt,
        ):
            answer = self._append_pending_approval_prompt(answer, pending_approval_prompt)
        if budget_exhausted:
            warning = "\n\n(注意：已达到步骤预算上限，任务可能未完成。) / (Warning: Step budget limit reached, the task may not be completed.)"
            answer = f"{answer}{warning}"
        self.runtime.memory.add_message(session_id, "assistant", answer)
        return AgentTurnResult(
            text=answer,
            session_id=session_id,
            run_id=run_id,
            action=action,
            observation=observation,
            model_role=model_role,
            terminal=True,
        )

    def _ensure_pending_approval_prompt(
        self,
        result: AgentTurnResult,
        pending_approval_prompt: str,
    ) -> AgentTurnResult:
        if not pending_approval_prompt or self._text_mentions_pending_approval(
            result.text,
            pending_approval_prompt,
        ):
            return result
        return AgentTurnResult(
            text=self._append_pending_approval_prompt(result.text, pending_approval_prompt),
            session_id=result.session_id,
            run_id=result.run_id,
            action=result.action,
            observation=result.observation,
            model_role=result.model_role,
            terminal=result.terminal,
            trace_id=result.trace_id,
        )

    @staticmethod
    def _append_pending_approval_prompt(text: str, pending_approval_prompt: str) -> str:
        text = text.strip()
        return f"{text}\n\n{pending_approval_prompt}" if text else pending_approval_prompt

    @staticmethod
    def _text_mentions_pending_approval(text: str, pending_approval_prompt: str) -> bool:
        if pending_approval_prompt in text:
            return True
        marker = "审批码: `"
        if marker not in pending_approval_prompt:
            return False
        code = pending_approval_prompt.split(marker, 1)[1].split("`", 1)[0]
        return bool(code and code in text)

    @staticmethod
    def _approval_prompt_from_facts(facts: dict[str, Any] | None, *, source: str = "") -> str:
        if not facts or facts.get("status") != "awaiting_approval":
            return ""
        approval = facts.get("approval")
        if not isinstance(approval, dict):
            return ""
        code = str(approval.get("code") or "").strip()
        if not code:
            return ""
        run_id = str(facts.get("run_id") or "").strip()
        expires_at = approval.get("expires_at")
        try:
            minutes = max(0, round((float(expires_at) - time.time()) / 60)) if expires_at else 0
        except (TypeError, ValueError):
            minutes = 0
        expiry = f"审批将在约 {minutes} 分钟后过期。" if minutes else "审批有过期时间，请尽快处理。"
        affordance = approval_surface_affordance(source)
        commands = affordance.get("approval_commands") if isinstance(affordance.get("approval_commands"), dict) else {}
        approve_command = _first_command(commands, "approve", "approve")
        reject_command = _first_command(commands, "reject", "reject")
        template = str(affordance.get("approval_template") or "")
        if not template:
            return ""
        return template.format(
            task_line=f"任务 ID: `{run_id}`" if run_id else "",
            code=code,
            expiry=expiry,
            approve_command=approve_command,
            reject_command=reject_command,
        ).strip()


def _first_command(commands: dict[str, Any], key: str, fallback: str) -> str:
    raw = commands.get(key)
    if isinstance(raw, list) and raw:
        return str(raw[0])
    return fallback
