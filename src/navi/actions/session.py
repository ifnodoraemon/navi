from __future__ import annotations

from typing import Any

from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from ..capability_contract import CAPABILITY_ACTION_APPROVAL
from ..approval_contract import APPROVAL_ACTION_SESSION_ELEVATION
from ..lifecycle import Phase, Governance, Resolution
from .helpers import (
    arg_text as _arg_text,
    fact_result as _fact_result,
    transition_facts as _transition_facts,
)
from ..memory import MemoryStore
from ..runs import RunStore


@capability("session_create")
class SessionCreateCapability(BaseCapability):
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        alias = _arg_text(args, "alias")
        session_id = MemoryStore(self.home).create_session(alias=alias or None)
        facts = {
            **_transition_facts("session", session_id, "created"),
            "session_id": session_id,
            "alias": alias,
        }
        return _fact_result("session", facts, run_id=session_id)


@capability("session_request_elevation")
class SessionRequestElevationCapability(BaseCapability):

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        target_permission = _arg_text(args, "target_permission")
        reason = _arg_text(args, "reason")

        runs = RunStore(self.home)
        existing = _existing_elevation_request(
            runs,
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            target_permission=target_permission,
        )
        if existing is not None:
            task, approval = existing
            facts = _elevation_facts(
                task_id=task.id,
                target_permission=target_permission,
                reason=reason,
                approval=approval,
                transition="existing",
            )
            return CapabilityResult(
                ok=False,
                action=CAPABILITY_ACTION_APPROVAL,
                message="",
                facts=facts,
                error_reason="session_elevation_requested",
                yields_control=True,
                terminal=False,
                run_id=task.id,
            )

        task = runs.create(
            f"Elevate session permission to {target_permission}. Reason: {reason}",
            kind="elevation",
            workspace=context.workspace,
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            phase=Phase.PAUSED,
            governance=Governance.AWAITING_APPROVAL,
            resolution=Resolution.BLOCKED,
        )
        approval = runs.create_approval(
            run_id=task.id,
            action=APPROVAL_ACTION_SESSION_ELEVATION,
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            requested_permission=target_permission,
            reason=reason,
        )
        task = runs.update_run(
            task.id,
            plan_summary=f"session_elevation:{target_permission}",
            result_summary="",
        ) or task
        facts = _elevation_facts(
            task_id=task.id,
            target_permission=target_permission,
            reason=reason,
            approval=approval,
            transition="elevation_requested",
        )
        return CapabilityResult(
            ok=False,
            action=CAPABILITY_ACTION_APPROVAL,
            message="",
            facts=facts,
            error_reason="session_elevation_requested",
            yields_control=True,
            terminal=False,
            run_id=task.id,
        )


def _existing_elevation_request(
    runs: RunStore,
    *,
    source: str,
    peer_id: str,
    sender_id: str,
    target_permission: str,
):
    for run in runs.list(limit=100):
        if run.kind != "elevation" or run.phase == Phase.ENDED:
            continue
        if run.source != source or run.peer_id != peer_id or run.sender_id != sender_id:
            continue
        if run.plan_summary != f"session_elevation:{target_permission}":
            continue
        approval = runs.pending_approval_for_run(
            run.id,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            action=APPROVAL_ACTION_SESSION_ELEVATION,
            requested_permission=target_permission,
        )
        if approval is not None:
            return run, approval
    return None


def _elevation_facts(*, task_id: str, target_permission: str, reason: str, approval, transition: str) -> dict[str, Any]:
    return {
        "entity_type": "session",
        "entity_id": task_id,
        "state_transition": transition,
        "turn_scope": "current",
        "phase": Phase.PAUSED,
        "governance": Governance.AWAITING_APPROVAL,
        "resolution": Resolution.BLOCKED,
        "target_permission": target_permission,
        "reason": reason,
        "approval": {
            "id": approval.id,
            "action": approval.action,
            "code": approval.code,
            "expires_at": approval.expires_at,
        },
        "run_id": task_id,
    }
