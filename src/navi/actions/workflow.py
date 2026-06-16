from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..capabilities_types import CapabilityContext, CapabilityResult
from ..capabilities import build_capability_registry
from ..tools import ToolSpec
from .helpers import (
    arg_text as _arg_text,
    transition_facts as _transition_facts,
    fact_result as _fact_result,
    resolve_workspace as _resolve_workspace,
    positive_int as _positive_int,
    workflow_not_found as _workflow_not_found,
    json_list as _json_list,
)
from ..operating_context import permission_allows
from ..tools import WORKFLOW_STEP_CONTEXT
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


class WorkflowProposeCapability:
    def __init__(self, spec: ToolSpec, *, home: Path, project_dir: Path):
        self.spec = spec
        self.home = home
        self.project_dir = project_dir

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        objective = _arg_text(args, "objective")
        if not objective:
            return CapabilityResult(
                ok=False,
                action="workflow",
                observation="workflow.propose requires objective.",
                message="workflow.propose requires objective.",
                terminal=False,
                error_reason="schema_mismatch",
            )
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


class WorkflowApproveCapability:
    def __init__(self, spec: ToolSpec, *, home: Path):
        self.spec = spec
        self.home = home

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
            return CapabilityResult(
                ok=False,
                action="workflow",
                observation="workflow.approve requires decision approve or reject.",
                message="workflow.approve requires decision approve or reject.",
                terminal=False,
                error_reason="schema_mismatch",
            )
        store = WorkflowStore(self.home)
        workflow = store.get(workflow_id) if workflow_id else None
        if workflow is None:
            return _workflow_not_found(workflow_id)
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


class WorkflowRunCapability:
    def __init__(self, spec: ToolSpec, *, home: Path, project_dir: Path, resume: bool = False):
        self.spec = spec
        self.home = home
        self.project_dir = project_dir
        self.resume = resume

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
            return _workflow_not_found(workflow_id)
        if not workflow_can_run(workflow.status):
            return CapabilityResult(
                ok=False,
                action="workflow",
                observation=f"workflow {workflow.id} is {workflow.status}; approve or resume only supported running states.",
                message=f"workflow {workflow.id} is {workflow.status}; approve or resume only supported running states.",
                terminal=False,
                facts={"workflow_id": workflow.id, "status": workflow.status},
            )
        store.update_status(
            workflow.id,
            status=WORKFLOW_STATUS_RUNNING,
            evidence={"resume": self.resume, "runner": context.sender_id},
            event_type="workflow.resume" if self.resume else "workflow.run",
        )
        workflow = store.get(workflow.id) or workflow
        steps = store.list_steps(workflow.id)
        runnable = _runnable_steps(steps)[: workflow.max_concurrency]
        if not runnable:
            return self._finish_if_possible(store, workflow)
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
            return self._finish_if_possible(store, workflow)

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
            for call in tool_calls:
                tool_name = str(call.get("tool") or "").strip()
                if not tool_name:
                    continue
                if allowed_tools and tool_name not in allowed_tools:
                    raise ValueError(f"{tool_name} is not declared in step allowed_tools")
                requested_permission = str(call.get("permission") or "read")
                if not permission_allows(requested_permission, workflow.permission_ceiling):
                    raise ValueError(
                        f"{tool_name} requests {requested_permission}, above workflow ceiling {workflow.permission_ceiling}"
                    )
                registry = build_capability_registry(
                    self.home,
                    project_dir=Path(workflow.workspace),
                    permission_ceiling=workflow.permission_ceiling,
                    execution_context=WORKFLOW_STEP_CONTEXT,
                )
                spec = registry.get(tool_name)
                if spec is None:
                    raise ValueError(
                        f"{tool_name} is not available in execution context {WORKFLOW_STEP_CONTEXT}"
                    )
                capability_result = await registry.invoke(
                    tool_name,
                    call.get("args") if isinstance(call.get("args"), dict) else {},
                    permission=requested_permission,
                    context=CapabilityContext(
                        home=self.home,
                        peer_id=context.peer_id,
                        sender_id=context.sender_id,
                        source=context.source,
                        permission_ceiling=workflow.permission_ceiling,
                        workspace=workflow.workspace,
                    ),
                )
                evidence.append(
                    {
                        "tool": tool_name,
                        "permission": requested_permission,
                        "ok": capability_result.ok,
                        "facts": capability_result.facts or {},
                        "error": ""
                        if capability_result.ok
                        else capability_result.message or capability_result.observation,
                    }
                )
                if not capability_result.ok:
                    raise ValueError(capability_result.message or capability_result.observation)
            if not evidence:
                evidence.append(
                    {
                        "kind": "subagent_step",
                        "summary": "Step completed as an explicit workflow role record; no capability calls were declared.",
                    }
                )
            output = {"step_id": step.id, "evidence": evidence}
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
            return CapabilityResult(
                ok=False, action="workflow", observation=str(exc), message=str(exc), facts=output
            )

    def _finish_if_possible(self, store: WorkflowStore, workflow: Workflow) -> CapabilityResult:
        counts = _workflow_counts(store, workflow)
        transition = workflow_idle_transition(counts)
        if transition is not None:
            if transition.status == WORKFLOW_STATUS_COMPLETED:
                from dataclasses import replace

                decision = workflow_verification_decision(
                    workflow=replace(workflow, status=WORKFLOW_STATUS_COMPLETED),
                    steps=store.list_steps(workflow.id),
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


class WorkflowVerifyCapability:
    def __init__(self, spec: ToolSpec, *, home: Path):
        self.spec = spec
        self.home = home

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
            return _workflow_not_found(workflow_id)
        subagents = SubagentRunStore(self.home)
        verifier = subagents.start(
            role="critic",
            phase="workflow.verify",
            run_id=workflow.id,
            command=["navi", "workflow", "verify", workflow.id],
            input_data={"workflow_id": workflow.id, "strategy": workflow.verification_strategy},
        )
        decision = workflow_verification_decision(
            workflow=workflow,
            steps=store.list_steps(workflow.id),
        )
        subagents.finish(
            verifier.id,
            status="completed" if decision.passed else "failed",
            output_data=decision.output,
            error=decision.blocked_reason,
        )
        updated = (
            store.update_status(
                workflow.id,
                status=decision.status,
                blocked_reason=decision.blocked_reason,
                evidence={"verifier": decision.output, "verifier_subagent_id": verifier.id},
                event_type=decision.event_type,
            )
            or workflow
        )
        facts = {
            **_transition_facts("workflow", workflow.id, "updated"),
            "workflow_id": workflow.id,
            "status": updated.status,
            "verifier_passed": decision.passed,
            "blocked_reason": decision.blocked_reason,
        }
        return _fact_result("workflow", facts, run_id=workflow.id)


class WorkflowStatusCapability:
    def __init__(self, spec: ToolSpec, *, home: Path):
        self.spec = spec
        self.home = home

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
            return _workflow_not_found(workflow_id)
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


def _workflow_counts(store: WorkflowStore, workflow: Workflow) -> dict[str, int]:
    steps = store.list_steps(workflow.id)
    return {
        "step_count": len(steps),
        "pending_count": len([step for step in steps if step.status == STEP_STATUS_PENDING]),
        "completed_count": len([step for step in steps if step.status == STEP_STATUS_COMPLETED]),
        "failed_count": len([step for step in steps if step.status in {"failed", "blocked"}]),
    }
