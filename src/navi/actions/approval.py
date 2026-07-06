from __future__ import annotations

import json
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
        args_json = _canonical_args_json(args.get("args_json") or args.get("args"))
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
            result_summary=_approval_visible_text(approval),
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
        selection = _approval_selection(args, code=code, run_id=run_id)
        resolved = ApprovalService(self.home).resolve(
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
        )
        facts = resolved.facts
        return CapabilityResult(
            ok=resolved.ok,
            action="approval",
            observation=resolved.message,
            message="",
            run_id=str(facts.get("run_id") or ""),
            facts=facts,
            terminal=_approval_failure_is_terminal(facts),
            error_reason="" if resolved.ok else _approval_error_reason(facts),
        )


def _canonical_args_json(value: Any) -> str:
    if not isinstance(value, dict):
        if value is None:
            return "{}"
        try:
            value = json.loads(value)
            if not isinstance(value, dict):
                return json.dumps(value, sort_keys=True)
        except Exception:
            return str(value)
            
    filtered = {k: v for k, v in value.items() if k not in {"_thought", "thought", "reasoning", "rationale"}}
    from ..safeguards import redact_secrets_deep
    return json.dumps(redact_secrets_deep(filtered), ensure_ascii=False, sort_keys=True)


def _approval_visible_text(approval) -> str:
    return (
        "approval_requested\n"
        f"run_id={approval.run_id}\n"
        f"approval_id={approval.id}\n"
        f"action={approval.action}\n"
        f"approval_code={approval.code}\n"
        f"requested_tool={approval.requested_tool}\n"
        f"requested_permission={approval.requested_permission}\n"
        f"status={approval.status}"
    )


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
