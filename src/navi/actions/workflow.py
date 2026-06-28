from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from ..result import NotFound, SchemaMismatch, guarded
from ..capabilities import build_capability_registry
from ..tools import ToolSpec
from .helpers import (
    arg_text as _arg_text,
    transition_facts as _transition_facts,
    fact_result as _fact_result,
    resolve_workspace as _resolve_workspace,
    positive_int as _positive_int,
    json_list as _json_list,
    failure_result as _failure_result,
)
from ..tools import WORKFLOW_STEP_CONTEXT
from ..trace import LoopCheckResult, LoopDecision, TraceStore
from ..workflows import (
    STEP_STATUS_COMPLETED,
    STEP_STATUS_FAILED,
    STEP_STATUS_PENDING,
    STEP_STATUS_RUNNING,
    WORKFLOW_STATUS_APPROVED,
    WORKFLOW_STATUS_COMPLETED,
    WORKFLOW_STATUS_REJECTED,
    WORKFLOW_STATUS_RUNNING,
    Workflow,
    WorkflowStep,
    WorkflowStore,
    workflow_batch_transition,
    workflow_can_run,
    workflow_facts,
    workflow_idle_transition,
    workflow_verification_decision,
)
from ..subagents import SubagentRunStore


@capability("workflow_propose")
class WorkflowProposeCapability(BaseCapability):
    def __init__(self, spec: ToolSpec, *, home: Path, project_dir: Path):
        super().__init__(spec, home=home)
        self.project_dir = project_dir

    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        objective = _arg_text(args, "objective")
        if not objective:
            raise SchemaMismatch("workflow.propose requires objective.")
        store = WorkflowStore(self.home)
        workspace = _resolve_workspace(context.workspace, default=self.project_dir)
        steps = args.get("steps") if isinstance(args.get("steps"), list) else []
        plan = args.get("plan") if isinstance(args.get("plan"), dict) else {}
        workflow = store.create(
            objective=objective,
            workspace=workspace,
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            permission_ceiling=_arg_text(args, "permission_ceiling") or "read",
            max_concurrency=_positive_int(args.get("max_concurrency"), default=4, maximum=16),
            total_subagent_limit=_positive_int(
                args.get("total_subagent_limit"), default=32, maximum=1000
            ),
            risk_class=_arg_text(args, "risk_class"),
            estimated_cost=_arg_text(args, "estimated_cost"),
            stop_condition=_arg_text(args, "stop_condition"),
            verification_strategy=_arg_text(args, "verification_strategy"),
            plan=plan,
            steps=steps,
            evidence={
                "confirmation_required": True,
                "reason": "Dynamic workflows must be approved before execution.",
            },
        )
        facts = {
            **_transition_facts("workflow", workflow.id, "created"),
            "workflow_id": workflow.id,
            "status": workflow.status,
            "confirmation_required": True,
            "permission_ceiling": workflow.permission_ceiling,
            "max_concurrency": workflow.max_concurrency,
            "total_subagent_limit": workflow.total_subagent_limit,
            "step_count": len(store.list_steps(workflow.id)),
            "estimated_cost": workflow.estimated_cost,
            "risk_class": workflow.risk_class,
            "stop_condition": workflow.stop_condition,
        }
        return _fact_result("workflow", facts, run_id=workflow.id)


@capability("workflow_approve")
class WorkflowApproveCapability(BaseCapability):

    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        workflow_id = _arg_text(args, "workflow_id")
        decision = _arg_text(args, "decision").lower()
        if decision not in {"approve", "reject"}:
            raise SchemaMismatch("workflow.approve requires decision approve or reject.")
        store = WorkflowStore(self.home)
        workflow = store.get(workflow_id) if workflow_id else None
        if workflow is None:
            raise NotFound(f"workflow not found: {workflow_id}")
        if decision == "approve" and workflow.sender_id and context.sender_id != workflow.sender_id:
            message = (
                f"workflow {workflow.id} was created by sender "
                f"{workflow.sender_id}; only that sender may approve it."
            )
            return _failure_result(
                "workflow",
                message,
                error_reason="approver_not_creator",
                facts={
                    "workflow_id": workflow.id,
                    "creator_sender_id": workflow.sender_id,
                    "current_sender_id": context.sender_id,
                },
            )
        status = WORKFLOW_STATUS_APPROVED if decision == "approve" else WORKFLOW_STATUS_REJECTED
        updated = store.update_status(
            workflow.id,
            status=status,
            evidence={"approved_by": context.sender_id, "decision": decision},
            event_type=f"workflow.{decision}",
        )
        facts = {
            **_transition_facts("workflow", workflow.id, "updated"),
            "workflow_id": workflow.id,
            "status": status,
        }
        if updated:
            facts["workflow"] = workflow_facts(store, updated)["workflow"]
        return _fact_result("workflow", facts, run_id=workflow.id)


@capability("workflow_run")
class WorkflowRunCapability(BaseCapability):
    def __init__(self, spec: ToolSpec, *, home: Path, project_dir: Path):
        super().__init__(spec, home=home)
        self.project_dir = project_dir

    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        workflow_id = _arg_text(args, "workflow_id")
        store = WorkflowStore(self.home)
        workflow = store.get(workflow_id) if workflow_id else None
        if workflow is None:
            raise NotFound(f"workflow not found: {workflow_id}")
        if not workflow_can_run(workflow.status):
            raise SchemaMismatch(
                f"workflow {workflow.id} is {workflow.status}; "
                "approve or resume only supported running states."
            )
        resume = bool(args.get("resume"))
        store.update_status(
            workflow.id,
            status=WORKFLOW_STATUS_RUNNING,
            evidence={"resume": resume, "runner": context.sender_id},
            event_type="workflow.resume" if resume else "workflow.run",
        )
        workflow = store.get(workflow.id) or workflow
        steps = store.list_steps(workflow.id)
        runnable = _runnable_steps(steps)[: workflow.max_concurrency]
        if not runnable:
            return self._finish_if_possible(store, workflow, context=context)
        completed = 0
        failed = 0
        for step in runnable:
            result = await self._run_step(store, workflow, step, context=context)
            if result.ok:
                completed += 1
            else:
                failed += 1
                break
        workflow = store.get(workflow.id) or workflow
        pending_count = len(
            [step for step in store.list_steps(workflow.id) if step.status == STEP_STATUS_PENDING]
        )
        if pending_count == 0 and failed == 0:
            return self._finish_if_possible(store, workflow, context=context)

        transition = workflow_batch_transition(
            completed=completed,
            failed=failed,
            pending_count=pending_count,
        )
        workflow = (
            store.update_status(
                workflow.id,
                status=transition.status,
                blocked_reason=transition.blocked_reason,
                evidence=transition.evidence,
                event_type=transition.event_type,
            )
            or workflow
        )
        facts = {
            **_transition_facts("workflow", workflow.id, "updated"),
            "workflow_id": workflow.id,
            "status": workflow.status,
            **_workflow_counts(store, workflow),
        }
        return _fact_result("workflow", facts, run_id=workflow.id)

    async def _run_step(
        self,
        store: WorkflowStore,
        workflow: Workflow,
        step: WorkflowStep,
        *,
        context: CapabilityContext,
    ) -> CapabilityResult:
        subagents = SubagentRunStore(self.home)
        run = subagents.start(
            role=step.role,
            phase="workflow.step",
            run_id=workflow.id,
            command=["navi", "workflow", "step", workflow.id, step.id],
            input_data={
                "workflow_id": workflow.id,
                "step_id": step.id,
                "objective": step.objective,
                "permission_ceiling": workflow.permission_ceiling,
            },
        )
        store.update_step(step.id, status=STEP_STATUS_RUNNING, evidence={"subagent_id": run.id})
        tool_calls = _json_list(step.tool_calls_json)
        allowed_tools = set(_json_list(step.allowed_tools_json))
        evidence: list[dict[str, Any]] = []
        try:
            self._validate_declared_calls(
                tool_calls,
                allowed_tools=allowed_tools,
                workflow=workflow,
            )
            turn_result = await self._run_model_step(
                workflow,
                step,
                tool_calls=tool_calls,
                allowed_tools=allowed_tools,
                context=context,
            )
            evidence = self._trace_evidence(turn_result.trace_id)
            if not evidence:
                evidence.append(
                    {
                        "kind": "model_step",
                        "action": turn_result.action,
                        "ok": not bool(turn_result.facts and turn_result.facts.get("error_reason")),
                        "trace_id": turn_result.trace_id,
                        "summary": turn_result.text,
                    }
                )
            self._record_step_loop_decision(
                workflow,
                step,
                turn_result=turn_result,
                context=context,
            )
            if turn_result.action in {"ask", "ask.user"}:
                raise ValueError(turn_result.text or "workflow step requested user input")
            error_reason = ""
            if isinstance(turn_result.facts, dict):
                error_reason = str(turn_result.facts.get("error_reason") or "")
            if turn_result.action == "capability_error" or error_reason:
                raise ValueError(turn_result.text or error_reason or "workflow step failed")
            output = {
                "step_id": step.id,
                "trace_id": turn_result.trace_id,
                "action": turn_result.action,
                "summary": turn_result.text,
                "evidence": evidence,
            }
            subagents.finish(run.id, status="completed", output_data=output)
            store.update_step(step.id, status=STEP_STATUS_COMPLETED, evidence=output)
            return CapabilityResult(
                ok=True,
                action="workflow",
                observation=json.dumps(output, ensure_ascii=False),
                facts=output,
            )
        except Exception as exc:
            output = {"step_id": step.id, "evidence": evidence, "error": str(exc)}
            subagents.finish(run.id, status="failed", output_data=output, error=str(exc))
            store.update_step(step.id, status=STEP_STATUS_FAILED, evidence=output, error=str(exc))
            return _failure_result(
                "workflow",
                str(exc),
                error_reason="workflow_step_failed",
                facts={
                    "step_id": step.id,
                    "evidence": evidence,
                    "error_type": exc.__class__.__name__,
                },
            )

    def _validate_declared_calls(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        allowed_tools: set[str],
        workflow: Workflow,
    ) -> None:
        declared_tools = {
            str(call.get("tool") or "").strip()
            for call in tool_calls
            if isinstance(call, dict) and str(call.get("tool") or "").strip()
        }
        for tool_name in sorted(declared_tools):
            if tool_name not in allowed_tools:
                raise ValueError(f"{tool_name} is not declared in step allowed_tools")
        if not declared_tools:
            return
        registry = build_capability_registry(
            self.home,
            project_dir=Path(workflow.workspace),
            allowed_tools=allowed_tools,
            permission_ceiling=workflow.permission_ceiling,
            execution_context=WORKFLOW_STEP_CONTEXT,
        )
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            tool_name = str(call.get("tool") or "").strip()
            if not tool_name:
                continue
            requested_permission = str(call.get("permission") or "read")
            spec = registry.get(tool_name)
            if spec is None:
                raise ValueError(
                    f"{tool_name} is not available in execution context {WORKFLOW_STEP_CONTEXT}"
                )
            if not self._permission_allowed(requested_permission, workflow.permission_ceiling):
                raise ValueError(
                    f"{tool_name} requests {requested_permission}, above workflow ceiling {workflow.permission_ceiling}"
                )

    async def _run_model_step(
        self,
        workflow: Workflow,
        step: WorkflowStep,
        *,
        tool_calls: list[dict[str, Any]],
        allowed_tools: set[str],
        context: CapabilityContext,
    ):
        from ..config import load_config
        from ..engine import HernessEngine
        from ..provider import build_provider
        from ..runtime import AgentRuntime

        runtime = AgentRuntime(home=self.home, provider=build_provider(load_config(self.home).model))
        model_tools = set(allowed_tools)
        model_tools.update({"final.answer", "ask.user"})
        engine = HernessEngine(
            home=self.home,
            runtime=runtime,
            project_dir=Path(workflow.workspace),
            allowed_tools=model_tools,
            permission_ceiling=workflow.permission_ceiling,
            event_bus=context.event_bus,
            execution_context=WORKFLOW_STEP_CONTEXT,
            governed_workflow_id=workflow.id,
        )
        return await engine.handle(
            _step_prompt(
                workflow,
                step,
                tool_calls=tool_calls,
                allowed_tools=sorted(allowed_tools),
            ),
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            source=context.source,
            session_id=context.session_id,
            intent_facts={
                "workflow_id": workflow.id,
                "workflow_step_id": step.id,
                "workflow_step_role": step.role,
                "workflow_step_allowed_tools": sorted(allowed_tools),
                "declared_tool_calls": tool_calls,
            },
        )

    def _trace_evidence(self, trace_id: str) -> list[dict[str, Any]]:
        if not trace_id:
            return []

        evidence: list[dict[str, Any]] = []
        for event in TraceStore(self.home).list_events(trace_id):
            if event.phase != "capability.result":
                continue
            evidence.append(
                {
                    "tool": event.tool,
                    "ok": event.ok,
                    "input": _json_object(event.input_json),
                    "output": _json_object(event.output_json),
                    "message": event.message,
                    "model_role": event.model_role,
                }
            )
        return evidence

    def _record_step_loop_decision(
        self,
        workflow: Workflow,
        step: WorkflowStep,
        *,
        turn_result,
        context: CapabilityContext,
    ) -> None:
        if not turn_result.trace_id:
            return
        decision = "finalize"
        reason = "workflow_step_completed"
        check_passed = True
        severity = "info"
        next_action = "complete_step"
        if turn_result.action in {"ask", "ask.user"}:
            decision = "blocked"
            reason = "workflow_step_requested_user_input"
            check_passed = False
            severity = "error"
            next_action = "block_workflow_step"
        error_reason = ""
        if isinstance(turn_result.facts, dict):
            error_reason = str(turn_result.facts.get("error_reason") or "")
        if turn_result.action == "capability_error" or error_reason:
            decision = "failed"
            reason = "workflow_step_capability_failure"
            check_passed = False
            severity = "error"
            next_action = "fail_workflow_step"
        trace = TraceStore(self.home)
        trace.add_loop_decision(
            trace_id=turn_result.trace_id,
            decision=LoopDecision(
                decision=decision,
                reason=reason,
                phase="workflow.step",
                tool=turn_result.action,
                run_id=turn_result.run_id,
                workflow_id=workflow.id,
                step_id=step.id,
                checker_results=(
                    LoopCheckResult(
                        name="workflow_step_checker",
                        passed=check_passed,
                        severity=severity,
                        reason=turn_result.text or reason,
                    ),
                ),
                next_action=next_action,
            ),
            session_id=context.session_id or "",
            run_id=turn_result.run_id or workflow.id,
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
        )
        trace.evaluate_trace(turn_result.trace_id)

    @staticmethod
    def _permission_allowed(requested: str, ceiling: str) -> bool:
        from ..operating_context import permission_allows

        return permission_allows(requested, ceiling)

    def _finish_if_possible(
        self,
        store: WorkflowStore,
        workflow: Workflow,
        *,
        context: CapabilityContext,
    ) -> CapabilityResult:
        counts = _workflow_counts(store, workflow)
        transition = workflow_idle_transition(counts)
        if transition is not None:
            if transition.status == WORKFLOW_STATUS_COMPLETED:
                from dataclasses import replace

                decision = workflow_verification_decision(
                    workflow=replace(workflow, status=WORKFLOW_STATUS_COMPLETED),
                    steps=store.list_steps(workflow.id),
                )
                self._record_workflow_verification_loop_decision(
                    workflow,
                    decision=decision,
                    context=context,
                )
                workflow = (
                    store.update_status(
                        workflow.id,
                        status=decision.status,
                        blocked_reason=decision.blocked_reason,
                        evidence=decision.output,
                        event_type=decision.event_type,
                    )
                    or workflow
                )
            else:
                workflow = (
                    store.update_status(
                        workflow.id,
                        status=transition.status,
                        evidence=transition.evidence,
                        event_type=transition.event_type,
                    )
                    or workflow
                )
        facts = {
            **_transition_facts("workflow", workflow.id, "updated"),
            "workflow_id": workflow.id,
            "status": workflow.status,
            **counts,
        }
        return _fact_result("workflow", facts, run_id=workflow.id)

    def _record_workflow_verification_loop_decision(
        self,
        workflow: Workflow,
        *,
        decision,
        context: CapabilityContext,
    ) -> None:
        if not context.trace_id:
            return
        TraceStore(self.home).add_loop_decision(
            trace_id=context.trace_id,
            decision=LoopDecision(
                decision="finalize" if decision.passed else "blocked",
                reason="workflow_verifier_passed"
                if decision.passed
                else "workflow_verifier_blocked",
                phase="workflow.verify",
                tool="workflow.run",
                run_id=workflow.id,
                workflow_id=workflow.id,
                checker_results=tuple(
                    LoopCheckResult(
                        name=check.name,
                        passed=check.passed,
                        severity=check.severity,
                        reason=check.reason,
                        evidence=check.evidence,
                    )
                    for check in decision.check_results
                ),
                next_action="mark_workflow_verified" if decision.passed else "block_workflow",
                evidence=decision.output,
            ),
            session_id=context.session_id or "",
            run_id=workflow.id,
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
        )


@capability("workflow_status")
class WorkflowStatusCapability(BaseCapability):

    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        workflow_id = _arg_text(args, "workflow_id")
        store = WorkflowStore(self.home)
        workflow = store.get(workflow_id) if workflow_id else None
        if workflow is None:
            raise NotFound(f"workflow not found: {workflow_id}")
        facts = workflow_facts(store, workflow)
        return _fact_result("workflow", facts, run_id=workflow.id)


def _runnable_steps(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    completed_ids = {step.id for step in steps if step.status == STEP_STATUS_COMPLETED}
    runnable: list[WorkflowStep] = []
    for step in steps:
        if step.status != STEP_STATUS_PENDING:
            continue
        dependencies = set(_json_list(step.depends_on_json))
        if dependencies <= completed_ids:
            runnable.append(step)
    return runnable


def _step_prompt(
    workflow: Workflow,
    step: WorkflowStep,
    *,
    tool_calls: list[dict[str, Any]],
    allowed_tools: list[str],
) -> str:
    prompt = {
        "workflow_id": workflow.id,
        "workflow_objective": workflow.objective,
        "step_id": step.id,
        "step_role": step.role,
        "step_objective": step.objective,
        "permission_ceiling": workflow.permission_ceiling,
        "allowed_tools": allowed_tools,
        "depends_on": _json_list(step.depends_on_json),
        "declared_tool_calls": tool_calls,
        "instruction": (
            "Complete this workflow step by choosing from the current capability manifest. "
            "The declared_tool_calls are planner facts from the proposal, not a script to replay. "
            "Use final.answer when the step is complete, or ask.user only if user input is truly required."
        ),
    }
    return "Workflow step execution facts:\n" + json.dumps(
        prompt,
        ensure_ascii=False,
        sort_keys=True,
    )


def _json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _workflow_counts(store: WorkflowStore, workflow: Workflow) -> dict[str, int]:
    steps = store.list_steps(workflow.id)
    return {
        "step_count": len(steps),
        "pending_count": len([step for step in steps if step.status == STEP_STATUS_PENDING]),
        "completed_count": len([step for step in steps if step.status == STEP_STATUS_COMPLETED]),
        "failed_count": len([step for step in steps if step.status in {"failed", "blocked"}]),
    }
