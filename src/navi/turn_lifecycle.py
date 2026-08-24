"""Turn lifecycle helpers for the Navi control plane."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .goals import GoalStore

if TYPE_CHECKING:
    from .runtime import AgentRuntime


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
    memory_influence: tuple[str, ...] = ()
    facts: dict[str, Any] | None = None

    ok: bool = True
    yields_control: bool = False
    error_reason: str = ""

    def surfaced_text(self) -> str:
        """The text to surface to the user."""
        return self.text


class TurnLifecycleMixin:
    """Mixin providing turn lifecycle helpers for TurnController.

    Requires instance attributes (provided by TurnController.__init__):
    - home: Path
    - trace: TraceStore
    - runtime: AgentRuntime
    - capabilities: CapabilityRegistry
    - permission_ceiling: str
    - event_bus: Any | None
    """

    home: Path
    trace: Any  # TraceStore
    runtime: AgentRuntime
    capabilities: Any  # CapabilityRegistry
    permission_ceiling: str
    event_bus: Any | None
    governed_run_id: str
    _background_tasks: set[asyncio.Task[Any]]

    def _attach_goals(
        self, goal_ids: set[str], *, trace_id: str, session_id: str, evidence: dict[str, Any]
    ) -> None:
        if not goal_ids:
            return
        goals = GoalStore(self.home)
        for goal_id in sorted(goal_ids):
            goals.attach_trace(goal_id, trace_id=trace_id, session_id=session_id, evidence=evidence)

    def _trigger_background_memory(self, result: AgentTurnResult) -> None:
        if result.session_id and self.event_bus:
            task = asyncio.create_task(self._publish_turn_completed_event(result))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _publish_turn_completed_event(self, result: AgentTurnResult) -> None:
        """Publish turn completed event to event bus."""
        from .event_bus import AgentTurnCompletedEvent
        import logging

        logger = logging.getLogger("navi.control_plane")
        try:
            event_bus = self.event_bus
            assert event_bus is not None
            await event_bus.publish(
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
        trace_id: str = "",
        source: str = "",
        peer_id: str = "",
        sender_id: str = "",
    ) -> AgentTurnResult:
        session_id = session_id or self.runtime.memory.new_session_id()
        self.runtime.memory.add_message(
            session_id,
            "user",
            user_text,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            trace_id=trace_id,
            run_id=result.run_id,
        )
        surfaced_text = result.surfaced_text()
        if surfaced_text:
            self.runtime.memory.add_message(
                session_id,
                "assistant",
                surfaced_text,
                source=source,
                peer_id=peer_id,
                sender_id=sender_id,
                trace_id=trace_id,
                run_id=result.run_id,
            )
        self.runtime.memory.enqueue_consolidation(
            session_id=session_id,
            run_id=result.run_id or trace_id or result.session_id,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
        )
        return replace(
            result,
            session_id=session_id,
        )

    @staticmethod
    def _with_trace(result: AgentTurnResult, trace_id: str) -> AgentTurnResult:
        if result.trace_id == trace_id:
            return result
        return replace(result, trace_id=trace_id)

    def _record_trace_final(
        self,
        result: AgentTurnResult,
        trace_id: str,
        *,
        source: str,
        peer_id: str,
        sender_id: str,
    ) -> None:
        surfaced_text = result.surfaced_text()
        self.trace.add_event(
            trace_id=trace_id,
            phase="turn.final",
            session_id=result.session_id,
            run_id=result.run_id,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            model_role=result.model_role,
            ok=result.ok,
            output_data={
                "action": result.action,
                "terminal": result.terminal,
                "error_reason": result.error_reason,
            },
            message=surfaced_text,
        )
        self.trace.evaluate_trace(trace_id)
