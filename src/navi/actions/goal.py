from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ..capabilities_types import BaseCapability, CapabilityContext, CapabilityResult, capability
from ..goal_state_graph import run_goal_loop_state_graph
from ..loop_control_service import LoopControlService, OpenGoalRequest
from ..result import Conflict, NotFound, SchemaMismatch, guarded
from ..tools import ToolSpec
from .helpers import arg_text as _arg_text, fact_result as _fact_result, positive_int as _positive_int


@capability("goal_open")
class GoalOpenCapability(BaseCapability):
    def __init__(
        self,
        spec: ToolSpec,
        *,
        home: Path,
        project_dir: Path,
        runtime: Any | None = None,
    ):
        super().__init__(spec, home=home)
        self.project_dir = project_dir
        self.runtime = runtime

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
            raise SchemaMismatch("goal.open requires objective.")
        workspace = _arg_text(args, "workspace") or context.workspace or str(self.project_dir)
        request = OpenGoalRequest(
            objective=objective,
            workspace=workspace,
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            session_id=context.session_id or "",
            scope=_string_tuple(args.get("scope")),
            constraints=_string_tuple(args.get("constraints")),
            acceptance_criteria=_string_tuple(args.get("acceptance_criteria")),
            permission_ceiling=_arg_text(args, "permission_ceiling") or context.permission_ceiling,
            allowed_capabilities=_string_tuple(args.get("allowed_capabilities")),
            verification_command=_arg_text(args, "verification_command"),
            timeout_seconds=_positive_int(args.get("timeout_seconds"), default=120, maximum=3600),
            auto_start=bool(args.get("auto_start", True)),
            cron_schedule=_arg_text(args, "cron_schedule"),
        )
        try:
            service = LoopControlService(self.home)
            if request.auto_start and self.runtime is not None:
                opened = service.open_goal(replace(request, auto_start=False))
                result = await run_goal_loop_state_graph(
                    home=self.home,
                    service=service,
                    base=opened,
                    runtime=self.runtime,
                    planner_capabilities=_planner_capabilities(
                        self.home,
                        opened.goal.workspace,
                        permission_ceiling=request.permission_ceiling
                        or context.permission_ceiling,
                        runtime=self.runtime,
                    ),
                    context=context,
                    evidence={"entrypoint": "goal.open"},
                    result_evidence={"state_graph_mode": "llm_backed"},
                )
            else:
                result = service.open_goal(request)
        except ValueError as exc:
            raise SchemaMismatch(str(exc)) from exc
        return _fact_result("goal", result.to_facts(), run_id=result.run.id)


@capability("goal_resume")
class GoalResumeCapability(BaseCapability):
    def __init__(
        self,
        spec: ToolSpec,
        *,
        home: Path,
        project_dir: Path,
        runtime: Any | None = None,
    ):
        super().__init__(spec, home=home)
        self.project_dir = project_dir
        self.runtime = runtime

    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        goal_id = _arg_text(args, "goal_id")
        loop_run_id = _arg_text(args, "loop_run_id")
        if not goal_id and not loop_run_id:
            raise SchemaMismatch("goal.resume requires goal_id or loop_run_id.")
        workspace = _arg_text(args, "workspace") or context.workspace or str(self.project_dir)
        service = LoopControlService(self.home)
        try:
            if loop_run_id:
                prepared = service.resume_loop(loop_run_id=loop_run_id, workspace=workspace)
            else:
                prepared = service.resume_goal(goal_id=goal_id, workspace=workspace)
            if self.runtime is not None:
                result = await run_goal_loop_state_graph(
                    home=self.home,
                    service=service,
                    base=prepared,
                    runtime=self.runtime,
                    planner_capabilities=_planner_capabilities(
                        self.home,
                        prepared.goal.workspace,
                        permission_ceiling=context.permission_ceiling,
                        runtime=self.runtime,
                    ),
                    context=context,
                    evidence={"entrypoint": "goal.resume", "resumed": True},
                    result_evidence={"state_graph_mode": "llm_backed", "resumed": True},
                    state_transition="resumed",
                )
            else:
                result = prepared
        except KeyError as exc:
            raise NotFound(str(exc)) from exc
        except ValueError as exc:
            raise Conflict(str(exc)) from exc
        return _fact_result("goal", result.to_facts(), run_id=result.run.id)


@capability("goal_cancel")
class GoalCancelCapability(BaseCapability):
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
        goal_id = _arg_text(args, "goal_id")
        loop_run_id = _arg_text(args, "loop_run_id")
        if not goal_id and not loop_run_id:
            raise SchemaMismatch("goal.cancel requires goal_id or loop_run_id.")
        reason = _arg_text(args, "reason")
        service = LoopControlService(self.home)
        try:
            if loop_run_id:
                result = service.cancel_loop(loop_run_id=loop_run_id, reason=reason)
            else:
                result = service.cancel_goal(goal_id=goal_id, reason=reason)
        except KeyError as exc:
            raise NotFound(str(exc)) from exc
        except ValueError as exc:
            raise Conflict(str(exc)) from exc
        return _fact_result("goal", result.to_facts(), run_id=result.run.id)


@capability("goal_state")
class GoalStateCapability(BaseCapability):
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
        try:
            facts = LoopControlService(self.home).goal_state(
                goal_id=_arg_text(args, "goal_id"),
                loop_run_id=_arg_text(args, "loop_run_id"),
                limit=_positive_int(args.get("limit"), default=20, maximum=200),
            )
        except KeyError as exc:
            raise NotFound(str(exc)) from exc
        run_id = str(facts.get("run", {}).get("id") or "")
        return _fact_result("goal", facts, run_id=run_id)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, list | tuple):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _planner_capabilities(home: Path, workspace: str, *, permission_ceiling: str, runtime: Any):
    from ..capabilities import CapabilityRegistry

    return CapabilityRegistry(
        home=home,
        project_dir=Path(workspace),
        permission_ceiling=permission_ceiling,
        runtime=runtime,
    )
