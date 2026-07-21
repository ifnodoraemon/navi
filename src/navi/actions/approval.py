from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..approval_contract import (
    APPROVAL_ACTION_RUN_EXECUTION,
    APPROVAL_DECISIONS,
    APPROVAL_STATUS_PENDING,
)
from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from ..control import ApprovalService, SurfaceContext
from ..lifecycle import Phase, Governance, Resolution
from ..loop_contracts import LoopTerminalState
from ..result import NotFound, SchemaMismatch, guarded
from ..runs import RunStore
from .helpers import (
    approval_error_reason as _approval_error_reason,
    approval_failure_is_terminal as _approval_failure_is_terminal,
    approval_selection as _approval_selection,
    fact_result as _fact_result,
    arg_text as _arg_text,
)


@capability("approval_request")
class ApprovalRequestCapability(BaseCapability):
    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        run_id = _arg_text(args, "run_id")
        if not run_id:
            raise SchemaMismatch("approval.request requires run_id.")
        runs = RunStore(self.home)
        run = runs.get(run_id)
        if run is None:
            raise NotFound(f"run not found: {run_id}")

        action = _arg_text(args, "action") or APPROVAL_ACTION_RUN_EXECUTION
        requested_tool = _arg_text(args, "requested_tool")
        requested_permission = _arg_text(args, "requested_permission")
        args_json = _canonical_args_json(
            args.get("args_json") or args.get("args"),
            home=self.home,
        )
        reason = _arg_text(args, "reason")
        source = context.source or run.source
        peer_id = context.peer_id or run.peer_id
        sender_id = context.sender_id or run.sender_id

        existing = runs.pending_approval_for_run(
            run_id,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            action=action,
            requested_tool=requested_tool,
            requested_permission=requested_permission,
            args_json=args_json,
        )
        approval = existing or runs.create_approval(
            run_id=run_id,
            action=action,
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            requested_tool=requested_tool,
            requested_permission=requested_permission,
            args_json=args_json,
            reason=reason,
        )
        runs.update_run(
            run_id,
            phase=Phase.PAUSED,
            governance=Governance.AWAITING_APPROVAL,
            resolution=Resolution.BLOCKED,
            result_summary="",
            error="",
        )

        facts = {
            "entity_type": "approval_request",
            "entity_id": approval.id,
            "state_transition": "created" if existing is None else "existing",
            "turn_scope": "current",
            "run_id": run_id,
            "status": APPROVAL_STATUS_PENDING,
            "approval": _approval_facts(approval),
        }
        return _fact_result("approval", facts, run_id=run_id)


@capability("approval_resolve")
class ApprovalResolveCapability(BaseCapability):
    def __init__(self, spec, *, home: Path, runtime=None):
        super().__init__(spec, home=home)
        self.runtime = runtime

    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        decision = _arg_text(args, "decision").lower()
        if decision not in APPROVAL_DECISIONS:
            raise SchemaMismatch("approval.resolve requires decision approve or reject.")
        code = _arg_text(args, "code")
        run_id = _arg_text(args, "run_id")
        if "selection" in args:
            raise SchemaMismatch(
                "approval.resolve does not accept a replacement selection; changed tool arguments "
                "require a new approval"
            )
        selection = _approval_selection(args, code=code, run_id=run_id)
        resolved = await ApprovalService(self.home).resolve_and_continue(
            decision=decision,
            selection=selection,
            context=SurfaceContext(
                home=self.home,
                source=context.source,
                peer_id=context.peer_id,
                sender_id=context.sender_id,
                session_id=context.session_id,
                workspace=context.workspace,
                input_text=context.input_text,
            ),
            code=code,
            run_id=run_id,
            runtime=self.runtime,
            trace_id=context.trace_id,
            event_bus=context.event_bus,
        )
        facts = resolved.facts
        from ..connector_delivery import connector_delivery_from_facts

        delivery = connector_delivery_from_facts(facts)
        if delivery is not None:
            return CapabilityResult(
                ok=resolved.ok,
                action="connector_outbound",
                message=delivery.text,
                run_id=str(facts.get("run_id") or ""),
                facts=facts,
                terminal=False,
                error_reason="" if resolved.ok else _approval_error_reason(facts),
                yields_control=True,
            )
        continuation_status = str(facts.get("continuation_status") or "")
        yields_control = continuation_status in {
            str(LoopTerminalState.PAUSED),
            str(LoopTerminalState.WAITING_APPROVAL),
            "waiting_approval",
        }
        return CapabilityResult(
            ok=resolved.ok,
            action="approval",
            message="",
            run_id=str(facts.get("run_id") or ""),
            facts=facts,
            terminal=_approval_failure_is_terminal(facts),
            error_reason="" if resolved.ok else _approval_error_reason(facts),
            yields_control=yields_control,
        )


def _canonical_args_json(value: Any, *, home: Path) -> str:
    if not isinstance(value, dict):
        if value is None:
            return "{}"
        try:
            value = json.loads(value)
            if not isinstance(value, dict):
                from ..safeguards import canonical_approval_args_json

                return canonical_approval_args_json(value, home=home)
        except Exception:
            from ..safeguards import canonical_approval_args_json

            return canonical_approval_args_json(
                {"content": str(value)},
                home=home,
            )
    from ..safeguards import canonical_approval_args_json

    return canonical_approval_args_json(value, home=home)


def _approval_facts(approval) -> dict[str, Any]:
    return {
        "id": approval.id,
        "run_id": approval.run_id,
        "action": approval.action,
        "requested_tool": approval.requested_tool,
        "requested_permission": approval.requested_permission,
        "status": approval.status,
        "code": approval.code,
        "expires_at": approval.expires_at,
        "reason": approval.reason,
    }
