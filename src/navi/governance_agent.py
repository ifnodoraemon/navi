from __future__ import annotations

from pathlib import Path

from .event_bus import (
    ActionApprovedEvent,
    ActionRequestedEvent,
    ActionSuspendedEvent,
    EventBus,
)
from .governance import GovernanceEngine
from .runs import RunStore


class GovernanceAgent:
    def __init__(self, home: Path, event_bus: EventBus) -> None:
        self.home = home
        self.event_bus = event_bus
        self.governance = GovernanceEngine(home)
        self.runs = RunStore(home)
        self._subscribe()

    def _subscribe(self) -> None:
        self.event_bus.subscribe("action_requested", self._on_action_requested)

    async def _on_action_requested(self, event: ActionRequestedEvent) -> None:
        task = self.runs.get(event.run_id)
        if not task:
            return

        if self.governance.execution_allowed(task):
            await self.event_bus.publish(
                ActionApprovedEvent(
                    source_agent="governance_agent",
                    run_id=event.run_id,
                    reason="auto-approved",
                )
            )
        else:
            approval = self.runs.create_approval(
                run_id=event.run_id,
                peer_id=event.peer_id,
                sender_id=event.sender_id,
            )
            self.runs.update_run(event.run_id, status="awaiting_approval")
            await self.event_bus.publish(
                ActionSuspendedEvent(
                    source_agent="governance_agent",
                    correlation_id=event.correlation_id,
                    run_id=event.run_id,
                    reason="requires approval",
                    approval_code=approval.code,
                    peer_id=event.peer_id,
                    sender_id=event.sender_id,
                    source=event.source,
                )
            )
