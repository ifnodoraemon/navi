from __future__ import annotations

from typing import Any

from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from ..capability_contract import CAPABILITY_ACTION_APPROVAL
from ..lifecycle import RUN_STATUS_PENDING
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

        task = runs.create(
            f"Elevate session permission to {target_permission}. Reason: {reason}",
            kind="elevation",
            workspace=context.workspace,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            status=RUN_STATUS_PENDING,
        )
        task = runs.update_run(
            task.id,
            plan_summary=f"session_elevation:{target_permission}",
        )

        return CapabilityResult(
            ok=True,
            action=CAPABILITY_ACTION_APPROVAL,
            observation="",
            run_id=task.id,
            facts={
                "entity_type": "session",
                "entity_id": task.id,
                "state_transition": "elevation_requested",
                "turn_scope": "current",
                "status": RUN_STATUS_PENDING,
                "target_permission": target_permission,
                "reason": reason,
                "run_id": task.id,
            },
            terminal=True,
        )
