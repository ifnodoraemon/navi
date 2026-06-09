from __future__ import annotations

from pathlib import Path

from .connector_registry import approval_surface_affordance
from .event_bus import (
    ActionApprovedEvent,
    ActionRequestedEvent,
    ActionSuspendedEvent,
    ApprovalCodeEvent,
    ApprovalResolvedEvent,
    EventBus,
    ResponseReadyEvent,
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
        self.event_bus.subscribe("approval_code", self._on_approval_code)

    async def _on_action_requested(self, event: ActionRequestedEvent) -> None:
        task = self.runs.get(event.run_id)
        if not task:
            return

        if task.autonomy_level == "L3" or self.governance.execution_allowed(task):
            await self.event_bus.publish(ActionApprovedEvent(
                source_agent="governance_agent",
                run_id=event.run_id,
                reason="auto-approved",
            ))
        else:
            approval = self.runs.create_approval(
                run_id=event.run_id,
                peer_id=event.peer_id,
                sender_id=event.sender_id,
            )
            self.runs.update_run(event.run_id, status="awaiting_approval")
            affordance = approval_surface_affordance(event.source)
            template = str(affordance.get("approval_template") or "")
            try:
                message = template.format(
                    code=approval.code,
                    run_id=event.run_id,
                    approve_command=f"批准 {approval.code}",
                    reject_command=f"拒绝 {approval.code}",
                    task_line=event.run_id[:8],
                    expiry="",
                )
            except (KeyError, IndexError):
                message = f"审批码：{approval.code}"
            await self.event_bus.publish(ActionSuspendedEvent(
                source_agent="governance_agent",
                correlation_id=event.correlation_id,
                run_id=event.run_id,
                reason="requires approval",
                approval_code=approval.code,
                peer_id=event.peer_id,
                sender_id=event.sender_id,
                source=event.source,
            ))
            await self.event_bus.send_response(ResponseReadyEvent(
                source_agent="governance_agent",
                correlation_id=event.correlation_id,
                message_id="",
                peer_id=event.peer_id,
                sender_id=event.sender_id,
                text=message,
                source=event.source,
            ))

    async def _on_approval_code(self, event: ApprovalCodeEvent) -> None:
        status = "approved" if event.decision == "approve" else "rejected"
        approval = self.governance.resolve_code(
            code=event.code,
            sender_id=event.sender_id,
            status=status,
        )
        if not approval:
            await self.event_bus.send_response(ResponseReadyEvent(
                source_agent="governance_agent",
                correlation_id=event.correlation_id,
                peer_id=event.peer_id,
                sender_id=event.sender_id,
                text="审批码无效或已过期。",
                source=event.source,
            ))
            return

        if status == "approved":
            await self.event_bus.publish(ApprovalResolvedEvent(
                source_agent="governance_agent",
                run_id=approval.run_id,
                approval_id=approval.id,
                decision="approved",
                sender_id=event.sender_id,
            ))
            await self.event_bus.send_response(ResponseReadyEvent(
                source_agent="governance_agent",
                correlation_id=event.correlation_id,
                peer_id=event.peer_id,
                sender_id=event.sender_id,
                text=f"已批准任务 {approval.run_id}，将在后台执行。",
                source=event.source,
            ))
        else:
            await self.event_bus.send_response(ResponseReadyEvent(
                source_agent="governance_agent",
                correlation_id=event.correlation_id,
                peer_id=event.peer_id,
                sender_id=event.sender_id,
                text=f"已拒绝任务 {approval.run_id}。",
                source=event.source,
            ))
