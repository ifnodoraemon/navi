from __future__ import annotations

import json
from typing import Any

from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from ..result import NotFound, guarded
from .helpers import (
    arg_text as _arg_text,
    transition_facts as _transition_facts,
    fact_result as _fact_result,
    approval_selection as _approval_selection,
    approval_result_message as _approval_result_message,
    approval_failure_is_terminal as _approval_failure_is_terminal,
    approval_error_reason as _approval_error_reason,
    failure_result as _failure_result,
)
from ..runs import RunStore
from ..goals import GoalStore
from ..control import ApprovalService, SurfaceContext


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
        run_id = _arg_text(args, "run_id") or _arg_text(args, "task_id")
        runs = RunStore(self.home)
        task = runs.get(run_id) if run_id else None
        if task is None:
            raise NotFound(f"delegation run not found: {run_id}")
        approval = runs.create_approval(
            run_id=task.id,
            peer_id=context.peer_id or task.peer_id,
            sender_id=context.sender_id or task.sender_id,
        )
        awaiting = runs.update_run(task.id, status="awaiting_approval") or task
        GoalStore(self.home).update_for_run(
            awaiting,
            evidence={
                "run_id": awaiting.id,
                "run_status": awaiting.status,
                "approval_status": approval.status,
            },
        )
        facts = {
            **_transition_facts("approval_request", approval.id, "created"),
            "run_id": awaiting.id,
            "status": awaiting.status,
            "approval": {
                "action": approval.action,
                "code": approval.code,
                "expires_at": approval.expires_at,
            },
        }
        return _fact_result(
            "approval",
            facts,
            run_id=awaiting.id,
        )


@capability("approval_resolve")
class ApprovalResolveCapability(BaseCapability):

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        decision = _arg_text(args, "decision").lower()
        code = _arg_text(args, "code")
        run_id = _arg_text(args, "run_id") or _arg_text(args, "task_id")
        selection = _approval_selection(args, code=code, run_id=run_id)
        if code and context.input_text and code not in context.input_text:
            facts = {
                "reason": "approval_code_not_in_user_input",
                "selection": selection,
                "code_present_in_current_user_input": False,
            }
            return _failure_result(
                "approval",
                message="approval code was not present in current user input",
                error_reason="schema_mismatch",
                terminal=True,
                facts=facts,
            )

        surface_ctx = SurfaceContext(
            home=self.home,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            source=context.source,
            workspace=context.workspace,
            session_id=context.session_id,
            input_text=context.input_text,
        )
        service = ApprovalService(self.home)
        res = service.resolve(
            decision=decision,
            selection=selection,
            context=surface_ctx,
            code=code,
            run_id=run_id,
        )
        message = _approval_result_message(res.message, res.facts)
        if not res.ok:
            return _failure_result(
                "approval",
                message,
                error_reason=_approval_error_reason(res.facts),
                run_id=res.run_id,
                terminal=_approval_failure_is_terminal(res.facts),
                facts=res.facts,
            )
        facts = res.facts or {}
        return CapabilityResult(
            ok=True,
            action="approval",
            observation=json.dumps(facts, ensure_ascii=False, sort_keys=True),
            message=message,
            run_id=res.run_id,
            facts=facts,
        )
