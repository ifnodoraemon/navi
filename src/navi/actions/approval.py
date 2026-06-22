from __future__ import annotations

from pathlib import Path
from typing import Any

from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from ..tools import ToolSpec
from .helpers import (
    arg_text as _arg_text,
    transition_facts as _transition_facts,
    fact_result as _fact_result,
    approval_selection as _approval_selection,
    approval_result_message as _approval_result_message,
    approval_failure_is_terminal as _approval_failure_is_terminal,
    approval_error_reason as _approval_error_reason,
)
from ..runs import RunStore
from ..goals import GoalStore
from ..control import ApprovalService, SurfaceContext


@capability("approval_request")
class ApprovalRequestCapability(BaseCapability):

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
            return CapabilityResult(
                ok=False,
                action="approval",
                observation=f"delegation run not found: {run_id}",
                message=f"delegation run not found: {run_id}",
                terminal=False,
                error_reason="not_found",
            )
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
            return CapabilityResult(
                ok=False,
                action="approval",
                observation="User did not provide this approval code. Do not hallucinate approvals.",
                message="User did not provide this approval code. Do not hallucinate approvals.",
                terminal=True,
                error_reason="schema_mismatch",
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
        return CapabilityResult(
            ok=res.ok,
            action="approval",
            observation=message,
            message=message,
            run_id=res.run_id,
            terminal=_approval_failure_is_terminal(res.facts),
            error_reason=_approval_error_reason(res.facts) if not res.ok else "unknown",
            facts=res.facts,
        )
