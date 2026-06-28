"""Engine phases mixin for HernessEngine handling turn lifecycle management."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .control import CurrentStateBuilder, SurfaceContext
from .engine_approval_prompts import _render_approval_prompt
from .engine_types import AgentTurnResult
from .goals import GoalStore
from .operating_context import OperatingContext
from .provider import ChatMessage
from .recovery import CompletionBlock, RecoveryPlanner
from .specs_data import RESPONDER_OBSERVATIONS_PROMPT
from .trace import TraceStore

if TYPE_CHECKING:
    from .runtime import AgentRuntime


class EnginePhasesMixin:
    """Mixin providing turn lifecycle phases for HernessEngine.

    Requires instance attributes (provided by HernessEngine.__init__):
    - home: Path
    - trace: TraceStore
    - runtime: AgentRuntime
    - recovery_planner: RecoveryPlanner
    - capabilities: CapabilityRegistry
    - permission_ceiling: str
    - event_bus: Any | None
    """

    home: Path
    trace: TraceStore
    runtime: AgentRuntime
    recovery_planner: RecoveryPlanner
    capabilities: Any  # CapabilityRegistry
    permission_ceiling: str
    event_bus: Any | None
    governed_workflow_id: str

    def _attach_goals(
        self, goal_ids: set[str], *, trace_id: str, session_id: str, evidence: dict[str, Any]
    ) -> None:
        if not goal_ids:
            return
        goals = GoalStore(self.home)
        for goal_id in sorted(goal_ids):
            goals.attach_trace(goal_id, trace_id=trace_id, session_id=session_id, evidence=evidence)

    def _completion_block_reason(
        self,
        events: list[dict[str, Any]],
        *,
        state_context: SurfaceContext | None = None,
    ) -> CompletionBlock | None:
        if not events:
            events = []
        latest_run_status: dict[str, str] = {}
        for event in events:
            facts = event.get("facts")
            if not isinstance(facts, dict):
                continue
            run_id = str(facts.get("run_id") or facts.get("task_id") or "").strip()
            status = str(
                facts.get("status") or facts.get("run_status") or facts.get("task_status") or ""
            ).strip()
            if run_id and status:
                latest_run_status[run_id] = status
        for event in events:
            facts = event.get("facts")
            if not isinstance(facts, dict):
                continue
            if str(facts.get("entity_type") or "") != "delegation_run":
                continue
            run_id = str(facts.get("run_id") or facts.get("task_id") or "").strip()
            status = latest_run_status.get(run_id) or str(facts.get("status") or "").strip()
            if run_id and status in {"pending", "prepared"}:
                return CompletionBlock(
                    reason=(
                        "loop checker blocked final answer: "
                        f"delegation run {run_id} is still {status}."
                    ),
                    run_id=run_id,
                    run_status=status,
                )
        latest_cleanup_facts = next(
            (
                event.get("facts")
                for event in reversed(events)
                if isinstance(event.get("facts"), dict)
                and event.get("facts", {}).get("entity_type") == "bulk_delete"
                and "completion_evidence" in event.get("facts", {})
            ),
            None,
        )
        if (
            isinstance(latest_cleanup_facts, dict)
            and latest_cleanup_facts.get("completion_evidence") is False
        ):
            remaining = latest_cleanup_facts.get("remaining_count")
            return CompletionBlock(
                reason=(
                    "loop checker blocked final answer: "
                    f"bulk_delete completion_evidence=false with remaining_count={remaining}."
                ),
            )
        if state_context is not None:
            state = CurrentStateBuilder(self.home).build(state_context)
            for run in state.active_runs:
                if run.status in {"pending", "preparing", "prepared"}:
                    return CompletionBlock(
                        reason=(
                            "loop checker blocked final answer: "
                            f"delegation run {run.id} is still {run.status}."
                        ),
                        run_id=run.id,
                        run_status=run.status,
                    )
            for workflow in state.active_workflows:
                if workflow.id == self.governed_workflow_id:
                    continue
                if workflow.status in {"approved", "running", "interrupted"}:
                    return CompletionBlock(
                        reason=(
                            "loop checker blocked final answer: "
                            f"workflow {workflow.id} is still {workflow.status}."
                        ),
                    )
        return None

    def _trigger_background_memory(self, result: AgentTurnResult) -> None:
        if result.session_id and self.event_bus:
            task = asyncio.create_task(self._publish_turn_completed_event(result))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _publish_turn_completed_event(self, result: AgentTurnResult) -> None:
        """Publish turn completed event to event bus."""
        from .event_bus import AgentTurnCompletedEvent
        import logging

        logger = logging.getLogger("navi.engine")
        try:
            await self.event_bus.publish(
                AgentTurnCompletedEvent(
                    session_id=result.session_id,
                    run_id=result.run_id,
                    action=result.action,
                )
            )
        except Exception as e:
            logger.error(f"Failed to publish turn completed event: {e}", exc_info=True)

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
            memory_influence=result.memory_influence,
            facts=result.facts,
            approval_affordance=result.approval_affordance,
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
            memory_influence=result.memory_influence,
            facts=result.facts,
            approval_affordance=result.approval_affordance,
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
    ) -> AgentTurnResult:
        session_id = session_id or self.runtime.memory.new_session_id()
        observation = "\n\n".join(observations)
        self.runtime.memory.add_message(session_id, "user", user_text)
        messages = self.runtime.build_messages(
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
                RESPONDER_OBSERVATIONS_PROMPT,
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
            output_data={"response_chars": len(answer)},
            message=f"{model_role} synthesized response",
        )
        approval_affordance = ""
        if pending_approval_prompt and not self._text_mentions_pending_approval(
            answer,
            pending_approval_prompt,
        ):
            approval_affordance = pending_approval_prompt
        self.runtime.memory.add_message(session_id, "assistant", answer)
        return AgentTurnResult(
            text=answer,
            session_id=session_id,
            run_id=run_id,
            action=action,
            observation=observation,
            model_role=model_role,
            terminal=True,
            approval_affordance=approval_affordance,
        )

    def _with_approval_affordance(
        self,
        result: AgentTurnResult,
        pending_approval_prompt: str,
    ) -> AgentTurnResult:
        """Attach the pending approval affordance as a separate field.

        The model's ``text`` is preserved verbatim. The approval affordance
        is surfaced as a distinct trailing block at the surface boundary
        (see ``AgentTurnResult.surfaced_text``) rather than rewritten into
        the model's utterance.
        """
        if not pending_approval_prompt or self._text_mentions_pending_approval(
            result.text,
            pending_approval_prompt,
        ):
            return result
        return replace(result, approval_affordance=pending_approval_prompt)

    @staticmethod
    def _append_pending_approval_prompt(text: str, pending_approval_prompt: str) -> str:
        text = text.strip()
        return f"{text}\n\n{pending_approval_prompt}" if text else pending_approval_prompt

    @staticmethod
    def _text_mentions_pending_approval(text: str, pending_approval_prompt: str) -> bool:
        if pending_approval_prompt in text:
            return True
        import re
        match = re.search(r"`(\d{6})`", pending_approval_prompt)
        if match:
            code = match.group(1)
            return bool(code and code in text)
        return False

    @staticmethod
    def _approval_prompt_from_facts(facts: dict[str, Any] | None, *, source: str = "") -> str:
        return _render_approval_prompt(facts, source=source)
