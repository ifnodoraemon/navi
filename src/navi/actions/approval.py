from __future__ import annotations

import json
from typing import Any

from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from ..capability_contract import CAPABILITY_ACTION_APPROVAL
from ..lifecycle import RUN_STATUS_AWAITING_APPROVAL
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
from ..control import ApprovalService, CurrentStateBuilder, SurfaceContext
from ..control import current_state_facts, explicit_code_was_user_provided


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
        awaiting = runs.update_run(task.id, status=RUN_STATUS_AWAITING_APPROVAL) or task
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
        batch_id = _arg_text(args, "batch_id")
        selection = _approval_selection(args, code=code, run_id=run_id, batch_id=batch_id)
        surface_ctx = SurfaceContext(
            home=self.home,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            source=context.source,
            workspace=context.workspace,
            session_id=context.session_id,
            input_text=context.input_text,
        )
        evidence_failure = _approval_evidence_failure(
            self.home,
            surface_ctx=surface_ctx,
            selection=selection,
            code=code,
            run_id=run_id,
            batch_id=batch_id,
            approval_evidence=_arg_text(args, "approval_evidence")
            or _arg_text(args, "user_approval_evidence"),
        )
        if evidence_failure is not None:
            return evidence_failure

        service = ApprovalService(self.home)
        res = service.resolve(
            decision=decision,
            selection=selection,
            context=surface_ctx,
            code=code,
            run_id=run_id,
            batch_id=batch_id,
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
            action=CAPABILITY_ACTION_APPROVAL,
            observation=json.dumps(facts, ensure_ascii=False, sort_keys=True),
            message=message,
            run_id=res.run_id,
            facts=facts,
        )


def _approval_evidence_failure(
    home,
    *,
    surface_ctx: SurfaceContext,
    selection: str,
    code: str,
    run_id: str,
    batch_id: str,
    approval_evidence: str,
) -> CapabilityResult | None:
    """Validate that a model-owned approval syscall cites current user evidence.

    The model still decides whether the user's text is an approval. This guard
    only prevents approving from hidden/current-state facts without a quote or
    code present in the current user turn.
    """

    input_text = surface_ctx.input_text.strip()
    if not input_text:
        return None
    state = CurrentStateBuilder(home).build(surface_ctx)
    state_facts = current_state_facts(state)

    if selection == "explicit_code":
        if explicit_code_was_user_provided(code=code, input_text=input_text):
            return None
        return _failure_result(
            "approval",
            "approval code was not present in current user input",
            error_reason="approval_evidence_missing",
            terminal=True,
            facts={
                "reason": "approval_code_not_in_user_input",
                "selection": selection,
                "code_present_in_current_user_input": False,
                "visible_pending_approval_count": state_facts[
                    "visible_pending_approval_count"
                ],
                "visible_pending_approvals": state_facts["visible_pending_approvals"],
                "visible_approval_batches": state_facts["visible_approval_batches"],
            },
        )

    if _user_evidence_matches_current_input(
        input_text,
        approval_evidence=approval_evidence,
        run_id=run_id,
        batch_id=batch_id,
    ):
        return None

    return _failure_result(
        "approval",
        "approval.resolve requires explicit current-user approval evidence for non-code selections",
        error_reason="approval_evidence_missing",
        facts={
            "reason": "approval_user_evidence_required",
            "selection": selection,
            "run_id": run_id,
            "batch_id": batch_id,
            "approval_evidence_present_in_current_user_input": False,
            "visible_pending_approval_count": state_facts[
                "visible_pending_approval_count"
            ],
            "visible_pending_approvals": state_facts["visible_pending_approvals"],
            "visible_approval_batches": state_facts["visible_approval_batches"],
        },
    )


def _user_evidence_matches_current_input(
    input_text: str,
    *,
    approval_evidence: str,
    run_id: str,
    batch_id: str,
) -> bool:
    if approval_evidence and approval_evidence in input_text:
        return True
    if run_id and run_id in input_text:
        return True
    return bool(batch_id and batch_id in input_text)
