from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from ..result import NotFound, PermissionDenied, SchemaMismatch, guarded
from ..tools import ToolSpec
from .helpers import (
    arg_text as _arg_text,
    transition_facts as _transition_facts,
    fact_result as _fact_result,
    resolve_workspace as _resolve_workspace,
    positive_int as _positive_int,
    remote_source as _remote_source,
    failure_result as _failure_result,
)
from ..config import load_config
from ..execution import ExecutionService
from ..goals import GoalStore
from ..graph import GraphStore
from ..lifecycle import (
    RUN_ACTIVE_STATUSES,
    RUN_STATUS_FAILED,
    RUN_STATUS_PENDING,
    RUN_STATUS_RUNNING,
)
from ..runs import RunStore

REMOTE_DELETABLE_STATUSES = frozenset({RUN_STATUS_FAILED, RUN_STATUS_PENDING, "expired"})
REMOTE_DELETABLE_KINDS = frozenset({"watch", "delegation"})


@capability("delegate_spawn")
class DelegateSpawnCapability(BaseCapability):
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
        context_str = _arg_text(args, "context")
        plan = _arg_text(args, "plan")
        success_criteria = _arg_text(args, "success_criteria")

        if not objective or not context_str or not plan or not success_criteria:
            raise SchemaMismatch(
                "delegate.spawn requires objective, context, plan, and success_criteria."
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
        workspace = _resolve_workspace(_arg_text(args, "workspace") or context.workspace, default=self.project_dir)

        existing = self._existing_active_run(
            runs, objective=objective, prompt=prompt, workspace=workspace, context=context
        )
        if existing is not None:
            facts = {
                **_transition_facts("delegation_run", existing.id, "existing"),
                "run_id": existing.id,
                "status": existing.status,
                "autonomy_level": existing.autonomy_level,
                "trust_rule_id": existing.trust_rule_id,
                "deduplicated": True,
            }
            return _fact_result("delegation", facts, run_id=existing.id)

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
        graph.upsert("DelegationRun", task.id, {"objective": task.title, "status": task.status, "prompt": task.prompt})
        goals = GoalStore(self.home)
        goal = goals.create(
            objective=task.prompt,
            source=task.source,
            peer_id=task.peer_id,
            sender_id=task.sender_id,
            workspace=task.workspace,
            run_id=task.id,
            timeout=config.execution.timeout_seconds,
            max_retries=3,
            evidence={"run_id": task.id, "run_status": task.status, "autonomy_level": task.autonomy_level},
        )

        facts = {
            **_transition_facts("delegation_run", task.id, "created"),
            "goal_id": goal.id,
            "run_id": task.id,
            "status": task.status,
            "autonomy_level": task.autonomy_level,
            "trust_rule_id": task.trust_rule_id,
        }
        return _fact_result("delegation", facts, run_id=task.id)

    @staticmethod
    def _existing_active_run(runs: RunStore, *, objective: str, prompt: str, workspace: str, context: CapabilityContext):
        from ..control import run_matches_context

        for run in runs.list(limit=100):
            if run.kind != "delegation":
                continue
            if run.status not in RUN_ACTIVE_STATUSES:
                continue
            if run.workspace != workspace:
                continue
            if run.title != objective[:120] and run.prompt != prompt:
                continue
            if run_matches_context(run, context):
                return run
        return None


@capability("delegate_run")
class DelegateRunCapability(BaseCapability):
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
        queued = runs.update_run(task.id, status=RUN_STATUS_RUNNING) or task
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


@capability("delegate_send_input")
class DelegateSendInputCapability(BaseCapability):
    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        run_id = _arg_text(args, "run_id")
        message = _arg_text(args, "message")
        if not run_id or not message:
            raise SchemaMismatch("delegate.send_input requires run_id and message.")

        runs = RunStore(self.home)
        task = runs.get(run_id) if run_id else None
        if task is None:
            raise NotFound(f"delegation run not found: {run_id}")
        if task.status != RUN_STATUS_PENDING:
            return _failure_result(
                "delegation",
                message=f"Run {run_id} is not awaiting input (status={task.status}).",
                error_reason="not_awaiting_input",
                facts={"run_id": run_id, "status": task.status},
            )

        from ..memory import MemoryStore

        memory = MemoryStore(self.home)
        session_alias = f"executor:{run_id}"
        session_id = memory.current_session_id(session_alias)
        memory.add_message(session_id, "user", message)

        runs.update_run(task.id, status=RUN_STATUS_PENDING, result_summary="")

        return _fact_result(
            "delegation",
            {
                **_transition_facts("delegation_run", run_id, "updated"),
                "run_id": run_id,
                "status": RUN_STATUS_PENDING,
            },
            run_id=run_id,
        )


@capability("delegate_delete")
class DelegateDeleteCapability(BaseCapability):
    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        run_id = _arg_text(args, "run_id") or _arg_text(args, "task_id")
        reason = _arg_text(args, "reason")
        if not run_id:
            return self._delete_by_filter(args, context)
        runs = RunStore(self.home)
        graph = GraphStore(self.home)
        task = runs.get(run_id)
        if task is None:
            raise NotFound(f"delegation run not found: {run_id}")
        if _remote_source(context.source) and (
            task.status not in REMOTE_DELETABLE_STATUSES
            or task.kind not in REMOTE_DELETABLE_KINDS
        ):
            raise PermissionDenied(
                "remote delegate.delete can only delete delegation runs that are "
                "failed, pending, or expired."
            )
        deleted = runs.delete_run(run_id)
        if deleted is None:
            return _failure_result(
                "delegation",
                f"delegation run not found: {run_id}",
                error_reason="not_found",
                facts={"run_id": run_id},
            )
        graph.delete(deleted.id)
        facts = {
            **_transition_facts("delegation_run", deleted.id, "deleted"),
            "completion_evidence": True,
            "deleted": True,
            "run_id": deleted.id,
            "title": deleted.title,
            "status": deleted.status,
            "reason": reason,
        }
        return _fact_result("delegation", facts, run_id=deleted.id)

    def _delete_by_filter(self, args: dict[str, Any], context: CapabilityContext) -> CapabilityResult:
        status = _arg_text(args, "status") or "failed"
        raw_limit = args.get("limit")
        limit = (
            _positive_int(raw_limit, default=5000, maximum=5000) if raw_limit is not None else None
        )
        source = _arg_text(args, "source")
        kind = _arg_text(args, "kind")
        if not source and not kind:
            return _failure_result(
                "delegation",
                message="delegate.delete bulk cleanup requires source or kind scope.",
                error_reason="scope_required",
                facts={"status": status, "source": source, "kind": kind},
            )
        if _remote_source(context.source) and not kind:
            kind = "delegation"
        if _remote_source(context.source) and (
            status not in REMOTE_DELETABLE_STATUSES or kind not in REMOTE_DELETABLE_KINDS
        ):
            message = (
                "remote delegate.delete bulk cleanup requires status in "
                "{failed, pending, expired} and kind watch or delegation."
            )
            return _failure_result(
                "delegation",
                message,
                error_reason="remote_scope_denied",
                facts={"status": status, "source": source, "kind": kind},
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
            "completion_evidence": remaining_count == 0,
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


@capability("execution_retry")
class ExecutionRetryCapability(BaseCapability):
    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        run_id = _arg_text(args, "run_id") or _arg_text(args, "task_id")
        if not run_id:
            raise SchemaMismatch("delegate.retry requires run_id.")
        runs = RunStore(self.home)
        task = runs.get(run_id)
        if task is None:
            raise NotFound(f"delegation run not found: {run_id}")
        execution = ExecutionService(self.home)
        if not execution.execution_allowed(task):
            return _failure_result(
                "execution",
                message="execution grant missing: approved approval or explicit L3 trust rule required",
                error_reason="execution_grant_missing",
                facts={"run_id": run_id},
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


@capability("delegate_list")
class DelegateListCapability(BaseCapability):
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
