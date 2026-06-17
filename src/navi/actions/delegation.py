from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ..capabilities_types import CapabilityContext, CapabilityResult
from ..tools import ToolSpec
from .helpers import (
    arg_text as _arg_text,
    transition_facts as _transition_facts,
    fact_result as _fact_result,
    resolve_workspace as _resolve_workspace,
    positive_int as _positive_int,
    remote_source as _remote_source,
)
from ..config import load_config
from ..runs import RunStore
from ..graph import GraphStore
from ..goals import GoalStore
from ..execution import ExecutionService


# Statuses a remote surface (e.g. WeChat) is allowed to delete. A run stuck in
# awaiting_approval whose code has expired would otherwise be undeletable AND
# unapprovable — a dead end — so those terminal-for-the-user states are
# explicitly deletable from remote, not just `failed`.
REMOTE_DELETABLE_STATUSES = frozenset({"failed", "awaiting_approval", "expired"})
REMOTE_DELETABLE_KINDS = frozenset({"watch", "delegation"})


class DelegateSpawnCapability:
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
        context_str = _arg_text(args, "context")
        plan = _arg_text(args, "plan")
        success_criteria = _arg_text(args, "success_criteria")

        if not objective or not context_str or not plan or not success_criteria:
            return CapabilityResult(
                ok=False,
                action="delegation",
                observation="delegate.spawn requires objective, context, plan, and success_criteria.",
                message="delegate.spawn requires objective, context, plan, and success_criteria.",
                terminal=False,
                error_reason="schema_mismatch",
            )

        prompt = (
            f"Objective:\n{objective}\n\n"
            f"Context:\n{context_str}\n\n"
            f"Plan:\n{plan}\n\n"
            f"Success Criteria:\n{success_criteria}"
        )
        config = load_config(self.home)
        runs = RunStore(self.home)
        graph = GraphStore(self.home)
        workspace = _resolve_workspace(context.workspace, default=self.project_dir)

        task = runs.create(
            title=objective[:120],
            prompt=prompt,
            kind="delegation",
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            provider=config.execution.provider,
            workspace=workspace,
            autonomy_level="L2",
            trust_rule_id="",
            why_now="trigger=model_capability",
        )
        graph.upsert(
            "DelegationRun",
            task.id,
            {"objective": task.title, "status": task.status, "prompt": task.prompt},
        )
        goals = GoalStore(self.home)
        goal = goals.create(
            objective=task.prompt,
            source=task.source,
            peer_id=task.peer_id,
            sender_id=task.sender_id,
            workspace=task.workspace,
            run_id=task.id,
            evidence={
                "run_id": task.id,
                "run_status": task.status,
                "autonomy_level": task.autonomy_level,
            },
        )

        # Governance intercepts via event bus — creates approval if needed
        if context.event_bus is not None:
            from ..event_bus import ActionRequestedEvent

            await context.event_bus.publish(
                ActionRequestedEvent(
                    source_agent="main_agent",
                    correlation_id=task.id,
                    run_id=task.id,
                    peer_id=context.peer_id,
                    sender_id=context.sender_id,
                    source=context.source,
                    autonomy_level=task.autonomy_level,
                )
            )
            # Yield control so event bus worker can process the event and create approvals
            import asyncio
            await asyncio.sleep(0.05)
            task = runs.get(task.id) or task

        facts = {
            **_transition_facts("delegation_run", task.id, "created"),
            "goal_id": goal.id,
            "run_id": task.id,
            "status": task.status,
            "autonomy_level": task.autonomy_level,
            "trust_rule_id": task.trust_rule_id,
        }
        approvals = runs._approvals_for_run(task.id)
        if approvals:
            facts["approval"] = {
                "action": approvals[0].action,
                "code": approvals[0].code,
                "expires_at": approvals[0].expires_at,
            }

        return _fact_result(
            "delegation",
            facts,
            run_id=task.id,
        )


class DelegatePrepareCapability:
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
        run_id = _arg_text(args, "run_id") or _arg_text(args, "task_id")
        task = RunStore(self.home).get(run_id) if run_id else None
        if task is None:
            return CapabilityResult(
                ok=False,
                action="delegation",
                observation=f"delegation run not found: {run_id}",
                message=f"delegation run not found: {run_id}",
                terminal=False,
            )
        planned = await ExecutionService(self.home).plan_task(task)
        GoalStore(self.home).update_for_run(
            planned, evidence={"run_id": planned.id, "run_status": planned.status}
        )
        return _fact_result(
            "delegation",
            {
                **_transition_facts("delegation_run", planned.id, "updated"),
                "run_id": planned.id,
                "status": planned.status,
                "plan_summary": planned.plan_summary,
            },
            run_id=planned.id,
        )


class DelegateRunCapability:
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
        run_id = _arg_text(args, "run_id") or _arg_text(args, "task_id")
        runs = RunStore(self.home)
        task = runs.get(run_id) if run_id else None
        if task is None:
            return CapabilityResult(
                ok=False,
                action="delegation",
                observation=f"delegation run not found: {run_id}",
                message=f"delegation run not found: {run_id}",
                terminal=False,
            )
        execution = ExecutionService(self.home)
        if not execution.execution_allowed(task):
            return CapabilityResult(
                ok=False,
                action="delegation",
                observation="execution grant missing",
                message="execution grant missing",
                terminal=False,
            )
        queued = runs.update_run(task.id, status="queued") or task
        GoalStore(self.home).update_for_run(
            queued, evidence={"run_id": queued.id, "run_status": queued.status}
        )
        return _fact_result(
            "delegation",
            {
                **_transition_facts("delegation_run", queued.id, "updated"),
                "run_id": queued.id,
                "status": queued.status,
            },
            run_id=queued.id,
        )


class DelegateDeleteCapability:
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
        run_id = _arg_text(args, "run_id") or _arg_text(args, "task_id")
        reason = _arg_text(args, "reason")
        if not reason:
            return CapabilityResult(
                ok=False,
                action="delegation",
                observation="delegate.delete requires reason.",
                message="delegate.delete requires reason.",
                terminal=False,
                error_reason="schema_mismatch",
            )
        if not run_id:
            return self._delete_by_filter(args, context)
        runs = RunStore(self.home)
        graph = GraphStore(self.home)
        task = runs.get(run_id)
        if task is None:
            return CapabilityResult(
                ok=False,
                action="delegation",
                observation=f"delegation run not found: {run_id}",
                message=f"delegation run not found: {run_id}",
                terminal=False,
            )
        if _remote_source(context.source) and (
            task.status not in REMOTE_DELETABLE_STATUSES
            or task.kind not in REMOTE_DELETABLE_KINDS
        ):
            return CapabilityResult(
                ok=False,
                action="delegation",
                observation=(
                    "remote delegate.delete can only delete delegation runs that are "
                    "failed, awaiting_approval, or expired."
                ),
                message=(
                    "remote delegate.delete can only delete delegation runs that are "
                    "failed, awaiting_approval, or expired."
                ),
                terminal=False,
            )
        deleted = runs.delete_run(run_id)
        if deleted is None:
            return CapabilityResult(
                ok=False,
                action="delegation",
                observation=f"delegation run not found: {run_id}",
                message=f"delegation run not found: {run_id}",
                terminal=False,
            )
        graph.delete(deleted.id)
        facts = {
            **_transition_facts("delegation_run", deleted.id, "deleted"),
            "deleted": True,
            "run_id": deleted.id,
            "title": deleted.title,
            "status": deleted.status,
            "reason": reason,
        }
        return _fact_result(
            "delegation",
            facts,
            run_id=deleted.id,
        )

    def _delete_by_filter(self, args: dict[str, Any], context: CapabilityContext) -> CapabilityResult:
        status = _arg_text(args, "status") or "failed"
        raw_limit = args.get("limit")
        limit = (
            _positive_int(raw_limit, default=5000, maximum=5000) if raw_limit is not None else None
        )
        source = _arg_text(args, "source")
        kind = _arg_text(args, "kind")
        if not source and not kind:
            return CapabilityResult(
                ok=False,
                action="delegation",
                observation="delegate.delete bulk cleanup requires source or kind scope.",
                message="delegate.delete bulk cleanup requires source or kind scope.",
                terminal=False,
                error_reason="scope_required",
            )
        if _remote_source(context.source) and not kind:
            kind = "delegation"
        if _remote_source(context.source) and (
            status not in REMOTE_DELETABLE_STATUSES or kind not in REMOTE_DELETABLE_KINDS
        ):
            return CapabilityResult(
                ok=False,
                action="delegation",
                observation=(
                    "remote delegate.delete bulk cleanup requires status in "
                    "{failed, awaiting_approval, expired} and kind watch or delegation."
                ),
                message=(
                    "remote delegate.delete bulk cleanup requires status in "
                    "{failed, awaiting_approval, expired} and kind watch or delegation."
                ),
                terminal=False,
            )

        runs = RunStore(self.home)
        graph = GraphStore(self.home)
        before_count = runs.count_runs(status=status, source=source, kind=kind)
        candidates = runs.list_by_status_filtered(status, source=source, kind=kind, limit=limit)
        deleted = []
        for task in candidates:
            removed = runs.delete_run(task.id)
            if removed is None:
                continue
            graph.delete(removed.id)
            deleted.append(
                {
                    "run_id": removed.id,
                    "title": removed.title,
                    "source": removed.source,
                    "kind": removed.kind,
                    "updated_at": removed.updated_at,
                }
            )
        remaining_count = runs.count_runs(status=status, source=source, kind=kind)
        facts = {
            **_transition_facts("bulk_delete", "runs", "completed"),
            "entity_count": len(deleted),
            "before_count": before_count,
            "deleted_count": len(deleted),
            "deleted_runs": deleted,
            "remaining_count": remaining_count,
            "cleanup_complete": remaining_count == 0,
            "status_filter": status,
            "source_filter": source,
            "kind_filter": kind,
            "limit_filter": limit,
            "reason": _arg_text(args, "reason"),
        }
        return _fact_result("delegation", facts)


class ExecutionRetryCapability:
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
        run_id = _arg_text(args, "run_id") or _arg_text(args, "task_id")
        if not run_id:
            return CapabilityResult(
                ok=False,
                action="execution",
                observation="delegate.retry requires run_id.",
                message="delegate.retry requires run_id.",
                terminal=False,
                error_reason="schema_mismatch",
            )
        runs = RunStore(self.home)
        task = runs.get(run_id)
        if task is None:
            return CapabilityResult(
                ok=False,
                action="execution",
                observation=f"delegation run not found: {run_id}",
                message=f"delegation run not found: {run_id}",
                terminal=False,
            )
        execution = ExecutionService(self.home)
        if not execution.execution_allowed(task):
            return CapabilityResult(
                ok=False,
                action="execution",
                observation="execution grant missing: approved approval or explicit L3 trust rule required",
                message="execution grant missing: approved approval or explicit L3 trust rule required",
                terminal=False,
            )
        follow_up = _arg_text(args, "follow_up_prompt")
        retry_task = replace(
            task,
            prompt=f"{task.prompt}\n\nFollow-up execution instruction:\n{follow_up}"
            if follow_up
            else task.prompt,
        )
        result = await execution.execute_task(retry_task)
        facts = {
            **_transition_facts("execution_attempt", result.id, "created"),
            "run_id": result.id,
            "status": result.status,
            "result_summary": result.result_summary,
            "error": result.error,
        }
        return _fact_result("execution", facts, run_id=result.id)


class DelegateListCapability:
    """List delegation runs and watches scoped to the caller's context.

    delegate.list used to be a context-blind fact tool that returned every run
    across all channels, so a remote connector could see (and a user could try
    to approve) tasks created on other surfaces. As an action capability it has
    the SurfaceContext and filters to runs that match the caller's
    peer/sender/source, matching approval visibility (principles 4, 13, 16).
    """

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
        from dataclasses import asdict
        from ..control import run_matches_context

        limit = _positive_int(args.get("limit"), default=20, maximum=100)
        store = RunStore(self.home)
        runs = [run for run in store.list(limit=limit) if run_matches_context(run, context)]
        watches = [
            watch
            for watch in store.list_watches(limit=limit)
            if run_matches_context(watch, context)
        ]
        status_counts: dict[str, int] = {}
        for run in runs:
            status_counts[run.status] = status_counts.get(run.status, 0) + 1
        facts = {
            "runs": [asdict(run) for run in runs],
            "watches": [asdict(watch) for watch in watches],
            "run_status_counts": status_counts,
            "returned_run_count": len(runs),
            "run_limit": limit,
        }
        return _fact_result("delegation", facts)
