from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, replace
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
    budget_exhausted: bool = False
    memory_influence: tuple[str, ...] = ()


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


    def _get_effective_permission_ceiling(self, peer_id: str, sender_id: str) -> str:
        ceiling = self.permission_ceiling
        try:
            from navi.runs import RunStore
            runs = RunStore(self.home).list(limit=50)
            for r in runs:
                if r.peer_id == peer_id and r.sender_id == sender_id:
                    if r.plan_summary.startswith("session_elevation:"):
                        if r.status in ("queued", "completed", "active"):
                            elevated = r.plan_summary.split(":")[1]
                            return elevated
        except Exception:
            pass
        return ceiling

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
            permission_ceiling=self._get_effective_permission_ceiling(peer_id, sender_id),
            workspace=str(self.capabilities.gateway.project_dir.resolve()),
        )
        observations: list[str] = []
        completion_events: list[dict[str, Any]] = []
        goal_ids: set[str] = set()
        pending_approval_prompt = ""
        last_result: AgentTurnResult | None = None
        budget_exhausted = False
        for _ in range(self.step_budget):
            planner_specs = self.capabilities.planner_specs(permission_ceiling=context.permission_ceiling)
            valid_tools = {spec.name for spec in planner_specs}
            syscall = await self.planner.plan(
                text,
                tools=planner_specs,
                conversation_context=self._conversation_context(resolved_session_id),
                observations=observations,
                permission_ceiling=context.permission_ceiling,
                model_roles=self.runtime.model_roles(),
            )
            planner_ok = syscall.tool != "system.planner_error" and syscall.tool in valid_tools
            planner_message = syscall.reason
            if syscall.tool not in {"", "system.planner_error"} and syscall.tool not in valid_tools:
                planner_message = f"planner selected unavailable capability: {syscall.tool}"
            self.trace.add_event(
                trace_id=trace_id,
                phase="planner.syscall",
                session_id=resolved_session_id or "",
                source=source,
                peer_id=peer_id,
                sender_id=sender_id,
                tool=syscall.tool,
                model_role="planner",
                ok=planner_ok,
                input_data={"observations_count": len(observations), "permission_ceiling": context.permission_ceiling},
                output_data=asdict(syscall),
                message=planner_message,
            )
            if not planner_ok:
                if syscall.tool == "system.planner_error":
                    raise ValueError(f"failed to parse planner output: {syscall.reason}")
                break
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
                result = replace(
                    last_result,
                    text=result.text,
                    observation="\n\n".join(observations),
                    terminal=True,
                )
            if result.terminal and pending_approval_prompt:
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

        if budget_exhausted:
            pending_approval_prompt, last_result = await self._recover_budget_exhaustion(
                trace_id=trace_id,
                session_id=resolved_session_id or "",
                source=source,
                peer_id=peer_id,
                sender_id=sender_id,
                context=context,
                completion_events=completion_events,
                observations=observations,
                goal_ids=goal_ids,
                pending_approval_prompt=pending_approval_prompt,
                last_result=last_result,
            )

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
            messages = self.runtime.build_messages(
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
            self.runtime.memory.add_message(resolved_session_id, "assistant", answer)
            turn_res = AgentTurnResult(
                text=answer,
                session_id=resolved_session_id,
                action="chat",
                terminal=True,
                trace_id=trace_id,
                budget_exhausted=True,
            )
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

    async def _recover_budget_exhaustion(
        self,
        *,
        trace_id: str,
        session_id: str,
        source: str,
        peer_id: str,
        sender_id: str,
        context: CapabilityContext,
        completion_events: list[dict[str, Any]],
        observations: list[str],
        goal_ids: set[str],
        pending_approval_prompt: str,
        last_result: AgentTurnResult | None,
    ) -> tuple[str, AgentTurnResult | None]:
        self.trace.add_event(
            trace_id=trace_id,
            phase="runtime.budget_exhausted",
            session_id=session_id,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            model_role="runtime",
            ok=True,
            output_data={
                "observations_count": len(observations),
                "last_action": last_result.action if last_result else "",
                "last_run_id": last_result.run_id if last_result else "",
            },
            message="internal step budget exhausted",
        )
        for _ in range(2):
            recovery_plan = self.recovery.plan_budget_exhaustion(events=completion_events)
            choice = _safe_budget_recovery_choice(recovery_plan)
            self.trace.add_event(
                trace_id=trace_id,
                phase="recovery.plan",
                session_id=session_id,
                run_id=last_result.run_id if last_result else "",
                source=source,
                peer_id=peer_id,
                sender_id=sender_id,
                model_role="runtime",
                ok=True,
                input_data={"trigger": recovery_plan.trigger},
                output_data=asdict(recovery_plan),
                message=recovery_plan.recommended,
            )
            if choice is None:
                return pending_approval_prompt, last_result
            invoked = await self.capabilities.invoke(
                choice.tool,
                choice.args,
                permission=choice.permission,
                context=context,
            )
            completion_events.append(
                {
                    "tool": choice.tool,
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
                session_id=session_id,
                run_id=invoked.run_id,
                source=source,
                peer_id=peer_id,
                sender_id=sender_id,
                tool=choice.tool,
                model_role="runtime",
                ok=invoked.ok,
                input_data={"args": choice.args, "permission": choice.permission, "budget_recovery": True},
                output_data={"action": invoked.action, "facts": invoked.facts or {}, "terminal": invoked.terminal},
                message=invoked.message or invoked.observation,
            )
            observations.append(invoked.observation or invoked.message)
            approval_prompt = self._approval_prompt_from_facts(invoked.facts, source=source)
            if approval_prompt:
                pending_approval_prompt = approval_prompt
            last_result = AgentTurnResult(
                text=invoked.message or invoked.observation,
                run_id=invoked.run_id,
                action=invoked.action,
                observation=invoked.observation,
                model_role="runtime",
                terminal=invoked.terminal,
                budget_exhausted=True,
            )
            facts = invoked.facts or {}
            if not invoked.ok or facts.get("status") == "awaiting_approval":
                return pending_approval_prompt, last_result
        return pending_approval_prompt, last_result

    @staticmethod
    def _completion_block_reason(events: list[dict[str, Any]]) -> str:
        if not events:
            return ""
        latest_run_status: dict[str, str] = {}
        for event in events:
            facts = event.get("facts")
            if not isinstance(facts, dict):
                continue
            run_id = str(facts.get("run_id") or facts.get("task_id") or "").strip()
            status = str(facts.get("status") or facts.get("run_status") or facts.get("task_status") or "").strip()
            if run_id and status:
                latest_run_status[run_id] = status
        for event in events:
            if event.get("tool") != "delegate.spawn":
                continue
            facts = event.get("facts")
            if not isinstance(facts, dict):
                continue
            run_id = str(facts.get("run_id") or facts.get("task_id") or "").strip()
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
                    
                    # F06: Goal context compaction
                    from navi.goals import GoalStore
                    goal_store = GoalStore(self.home)
                    # For all active goals in this session
                    goals = goal_store.list(status="active")
                    for g in goals:
                        if g.session_id == result.session_id:
                            await goal_store.compact_events(g.id, self.runtime.provider)
                    
                    # Auto Eval Generation
                    try:
                        from navi.evolution import EvolutionEngine
                        evo = EvolutionEngine(self.home)
                        await evo.extract_evals_from_session(result.session_id)
                    except Exception as e:
                        logger.error(f"Background eval extraction failed: {e}", exc_info=True)

            task = asyncio.create_task(run_with_semaphore())
            self._background_tasks.add(task)
            def handle_done(t: asyncio.Task) -> None:
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
            budget_exhausted=result.budget_exhausted,
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
            budget_exhausted=result.budget_exhausted,
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
            output_data={
                "action": result.action,
                "terminal": result.terminal,
                "budget_exhausted": result.budget_exhausted,
            },
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
        messages = self.runtime.build_messages(
            session_id, user_text=user_text, operating_context=OperatingContext(
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
                        "Answer the user based on these facts, following your prompt layer rules.",
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
        self.runtime.memory.add_message(session_id, "assistant", answer)
        return AgentTurnResult(
            text=answer,
            session_id=session_id,
            run_id=run_id,
            action=action,
            observation=observation,
            model_role=model_role,
            terminal=True,
            budget_exhausted=budget_exhausted,
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
            budget_exhausted=result.budget_exhausted,
        )

    @staticmethod
    def _append_pending_approval_prompt(text: str, pending_approval_prompt: str) -> str:
        text = text.strip()
        return f"{text}\n\n{pending_approval_prompt}" if text else pending_approval_prompt

    @staticmethod
    def _text_mentions_pending_approval(text: str, pending_approval_prompt: str) -> bool:
        if pending_approval_prompt in text:
            return True
        marker = "Approval code: `"
        if marker not in pending_approval_prompt:
            return False
        code = pending_approval_prompt.split(marker, 1)[1].split("`", 1)[0]
        return bool(code and code in text)

    @staticmethod
    def _approval_prompt_from_facts(facts: dict[str, Any] | None, *, source: str = "") -> str:
        if not facts or facts.get("status") != "awaiting_approval":
            return ""
        workflow_id = str(facts.get("workflow_id") or "").strip()
        if workflow_id:
            step_count = facts.get("step_count")
            risk_class = str(facts.get("risk_class") or "unknown")
            estimated_cost = str(facts.get("estimated_cost") or "unknown")
            stop_condition = str(facts.get("stop_condition") or "").strip()
            details = [
                f"Workflow ID: `{workflow_id}`",
                f"Steps: {step_count}" if step_count is not None else "",
                f"Risk class: {risk_class}",
                f"Estimated cost: {estimated_cost}",
                f"Stop condition: {stop_condition}" if stop_condition else "",
            ]
            detail_text = "\n".join(f"- {item}" for item in details if item)
            return (
                "Workflow proposal is awaiting confirmation before execution.\n"
                f"{detail_text}\n"
                f"Approve: `navi workflow approve {workflow_id}`\n"
                f"Reject: `navi workflow reject {workflow_id}`"
            ).strip()
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
        expiry = f"Approval expires in ~{minutes} minutes." if minutes else "Approval is expiring soon."
        affordance = approval_surface_affordance(source)
        commands = affordance.get("approval_commands") if isinstance(affordance.get("approval_commands"), dict) else {}
        approve_command = _first_command(commands, "approve", "approve")
        reject_command = _first_command(commands, "reject", "reject")
        template = str(affordance.get("approval_template") or "")
        if not template:
            return ""
        return template.format(
            task_line=f"Task ID: `{run_id}`" if run_id else "",
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


def _safe_budget_recovery_choice(recovery_plan):
    if recovery_plan.recommended != "continue":
        return None
    allowed = {
        ("delegate.prepare", "prepare"),
        ("approval.request", "prepare"),
    }
    for choice in recovery_plan.choices:
        if choice.kind == "continue" and (choice.tool, choice.permission) in allowed:
            return choice
    return None
