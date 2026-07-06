from __future__ import annotations

from pathlib import Path

from .event_bus import (
    ActionApprovedEvent,
    ActionRequestedEvent,
    EventBus,
    NaviEvent,
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

    async def _on_action_requested(self, event: NaviEvent) -> None:
        assert isinstance(event, ActionRequestedEvent)
        task = self.runs.get(event.run_id)
        if not task:
            return

        if self.governance.execution_allowed(task):
            await self.event_bus.publish(
                ActionApprovedEvent(
                    source_agent="governance_agent",
                    run_id=event.run_id,
                    reason="execution_grant_allowed",
                )
            )
