from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from ..capabilities_types import BaseCapability, CapabilityContext, CapabilityResult, capability
from ..goal_state_graph import run_goal_loop_state_graph
from ..lifecycle import Governance, Phase, Resolution
from ..loop_control_service import (
    LoopControlService,
    OpenGoalRequest,
    ScheduleConflict,
    UpdateGoalRequest,
)
from ..loop_contracts import LoopTerminalState
from ..permission_contract import PERMISSION_ORDER, normalize_permission
from ..result import Conflict, NaviError, NotFound, PermissionDenied, SchemaMismatch, guarded
from ..tools import ToolSpec
from ..workspaces import workspaces_match
from .helpers import (
    arg_text as _arg_text,
    fact_result as _fact_result,
    failure_result as _failure_result,
    positive_int as _positive_int,
)


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
        loop_kind = _arg_text(args, "loop_kind") or "durable_goal"
        workspace = _arg_text(args, "workspace") or context.workspace or str(self.project_dir)
        if context.workspace and not workspaces_match(self.home, workspace, context.workspace):
            raise PermissionDenied("goal workspace does not match caller.")
        if loop_kind == "scheduled":
            from ..workspaces import ShadowWorkspaceManager

            workspace = ShadowWorkspaceManager(self.home).durable_workspace_for(
                workspace,
            )
        planner_capabilities = _planner_capabilities(
            self.home,
            workspace,
            context=context,
            runtime=self.runtime,
        )
        permission_ceiling = _goal_permission_ceiling(
            _arg_text(args, "permission_ceiling") or context.permission_ceiling,
            context=context,
        )
        requested_capabilities = _string_tuple(args.get("allowed_capabilities"))
        allowed_capabilities = _effective_allowed_capabilities(
            requested=requested_capabilities,
            context=context,
            registry=planner_capabilities,
            permission_ceiling=permission_ceiling,
        )
        request = OpenGoalRequest(
            objective=objective,
            workspace=workspace,
            loop_kind=loop_kind,
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            session_id=context.session_id or "",
            scope=_string_tuple(args.get("scope")),
            constraints=_string_tuple(args.get("constraints")),
            acceptance_criteria=_string_tuple(args.get("acceptance_criteria")),
            permission_ceiling=permission_ceiling,
            allowed_capabilities=allowed_capabilities,
            verification_command=_arg_text(args, "verification_command"),
            timeout_seconds=_positive_int(args.get("timeout_seconds"), default=120, maximum=3600),
            token_budget=_nonnegative_int(args.get("token_budget"), maximum=100_000_000),
            call_budget=_nonnegative_int(args.get("call_budget"), maximum=100_000),
            cost_budget=_nonnegative_float(args.get("cost_budget"), maximum=1_000_000.0),
            qps_limit=_nonnegative_int(args.get("qps_limit"), maximum=10_000),
            max_concurrent=_positive_int(args.get("max_concurrent"), default=1, maximum=100),
            auto_start=bool(args.get("auto_start", True)),
            cron_schedule=_arg_text(args, "cron_schedule"),
            parent_goal_id=_arg_text(args, "parent_goal_id"),
            allow_duplicate_schedule=bool(args.get("allow_duplicate_schedule", False)),
        )
        try:
            service = LoopControlService(self.home)
            if request.auto_start and self.runtime is not None and request.loop_kind != "scheduled":
                opened = service.open_goal(
                    replace(
                        request,
                        auto_start=False,
                        execution_mode="foreground",
                    )
                )
                result = await run_goal_loop_state_graph(
                    home=self.home,
                    service=service,
                    base=opened,
                    runtime=self.runtime,
                    planner_capabilities=planner_capabilities,
                    context=context,
                    evidence={"entrypoint": "goal.open"},
                    result_evidence={"state_graph_mode": "llm_backed"},
                )
            else:
                result = service.open_goal(request)
        except ScheduleConflict as exc:
            return _schedule_conflict_result(exc)
        except ValueError as exc:
            raise SchemaMismatch(str(exc)) from exc
        facts = result.to_facts()
        # Promote connector delivery contracts to the response boundary so the
        # active adapter can persist and deliver them through its durable transport.
        promoted = _promote_outbound_facts(result)
        if promoted:
            facts = {**facts, **promoted}
        responded_message = str(facts.get("responded_message") or "")
        return _goal_result(
            result=result,
            facts=facts,
            action="goal",
            message=responded_message,
        )


@capability("goal_update")
class GoalUpdateCapability(BaseCapability):
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
        if not goal_id:
            raise SchemaMismatch("goal.update requires goal_id.")
        service = LoopControlService(self.home)
        try:
            _require_goal_scope(
                service,
                goal_id=goal_id,
                loop_run_id="",
                context=context,
            )
            goal = service.goals.get(goal_id)
            if goal is None:
                raise NotFound(f"goal not found: {goal_id}")
            previous_spec = service.goal_loop_spec(goal_id)
            permission_ceiling = _goal_permission_ceiling(
                _arg_text(args, "permission_ceiling")
                or previous_spec.goal.permission_ceiling,
                context=context,
            )
            allowed_capabilities = _updated_allowed_capabilities(
                args,
                home=self.home,
                workspace=goal.workspace,
                context=context,
                runtime=self.runtime,
                permission_ceiling=permission_ceiling,
            )
            if allowed_capabilities is None:
                allowed_capabilities = _effective_allowed_capabilities(
                    requested=tuple(previous_spec.allowed_capabilities),
                    context=context,
                    registry=_planner_capabilities(
                        self.home,
                        goal.workspace,
                        context=context,
                        runtime=self.runtime,
                    ),
                    permission_ceiling=permission_ceiling,
                )
            result = service.update_goal(
                UpdateGoalRequest(
                    goal_id=goal_id,
                    objective=_arg_text(args, "objective"),
                    cron_schedule=_arg_text(args, "cron_schedule"),
                    scope=_optional_string_tuple(args, "scope"),
                    constraints=_optional_string_tuple(args, "constraints"),
                    acceptance_criteria=_optional_string_tuple(
                        args,
                        "acceptance_criteria",
                    ),
                    permission_ceiling=permission_ceiling,
                    allowed_capabilities=allowed_capabilities,
                    verification_command=(
                        _arg_text(args, "verification_command")
                        if "verification_command" in args
                        else None
                    ),
                    timeout_seconds=_optional_nonnegative_int(
                        args,
                        "timeout_seconds",
                        maximum=3600,
                        default=0,
                    ),
                    token_budget=_optional_nonnegative_int(
                        args,
                        "token_budget",
                        maximum=100_000_000,
                        default=-1,
                    ),
                    call_budget=_optional_nonnegative_int(
                        args,
                        "call_budget",
                        maximum=100_000,
                        default=-1,
                    ),
                    cost_budget=_optional_nonnegative_float(
                        args,
                        "cost_budget",
                        maximum=1_000_000.0,
                        default=-1.0,
                    ),
                    qps_limit=_optional_nonnegative_int(
                        args,
                        "qps_limit",
                        maximum=10_000,
                        default=-1,
                    ),
                    max_concurrent=_optional_nonnegative_int(
                        args,
                        "max_concurrent",
                        maximum=100,
                        default=0,
                    ),
                    allow_duplicate_schedule=bool(args.get("allow_duplicate_schedule", False)),
                )
            )
        except KeyError as exc:
            raise NotFound(str(exc)) from exc
        except ScheduleConflict as exc:
            return _schedule_conflict_result(exc)
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

    async def preflight(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult | None:
        del permission
        return _goal_control_preflight(self.home, args, context=context, operation="resume")

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
            _require_goal_scope(
                service,
                goal_id=goal_id,
                loop_run_id=loop_run_id,
                context=context,
            )
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
                        context=context,
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
        facts = result.to_facts()
        promoted = _promote_outbound_facts(result)
        if promoted:
            facts = {**facts, **promoted}
        return _goal_result(
            result=result,
            facts=facts,
            action="goal",
            message=str(facts.get("responded_message") or ""),
        )


@capability("goal_cancel")
class GoalCancelCapability(BaseCapability):
    def __init__(self, spec: ToolSpec, *, home: Path, project_dir: Path):
        super().__init__(spec, home=home)
        self.project_dir = project_dir

    async def preflight(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult | None:
        del permission
        return _goal_control_preflight(self.home, args, context=context, operation="cancel")

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
        goal_ids = _string_tuple(args.get("goal_ids"))
        reason = _arg_text(args, "reason")
        service = LoopControlService(self.home)
        if not goal_id and not loop_run_id and not goal_ids:
            raise SchemaMismatch(
                "goal.cancel requires goal_id, loop_run_id, or explicit goal_ids."
            )
        try:
            if goal_ids:
                if loop_run_id or goal_id:
                    raise SchemaMismatch(
                        "goal.cancel batch selectors cannot be combined with goal_id or loop_run_id."
                    )
                selected = [service.goals.get(item) for item in goal_ids]
                missing = [goal_id for goal_id, goal in zip(goal_ids, selected) if goal is None]
                goals = [goal for goal in selected if goal is not None]
                if missing:
                    raise NotFound("goal not found: " + ", ".join(missing))
                return _cancel_goal_batch(
                    service,
                    goals=goals,
                    context=context,
                    reason=reason,
                    selector={"goal_ids": list(goal_ids)},
                )
            _require_goal_scope(
                service,
                goal_id=goal_id,
                loop_run_id=loop_run_id,
                context=context,
            )
            if loop_run_id:
                result = service.cancel_loop(loop_run_id=loop_run_id, reason=reason)
            else:
                result = service.cancel_goal(goal_id=goal_id, reason=reason)
        except KeyError as exc:
            raise NotFound(str(exc)) from exc
        except ValueError as exc:
            raise Conflict(str(exc)) from exc
        facts = _with_verified_goal(service, result.to_facts(), result.goal.id)
        return _fact_result("goal", facts, run_id=result.run.id)


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
        goal_id = _arg_text(args, "goal_id")
        loop_run_id = _arg_text(args, "loop_run_id")
        parent_goal_id = _arg_text(args, "parent_goal_id")
        created_after = _nonnegative_float(
            args.get("created_after"), maximum=1_000_000_000_000.0
        )
        created_before = _nonnegative_float(
            args.get("created_before"), maximum=1_000_000_000_000.0
        )
        view = (_arg_text(args, "view") or "current").lower()
        if view not in {
            "inbox",
            "current",
            "scheduled",
            "occurrences",
            "pending_approval",
            "history",
        }:
            raise SchemaMismatch(
                "goal.state view must be inbox, current, scheduled, occurrences, "
                "pending_approval, or history."
            )
        if parent_goal_id and view != "occurrences":
            raise SchemaMismatch("goal.state parent_goal_id requires view=occurrences.")
        if (created_after or created_before) and view != "occurrences":
            raise SchemaMismatch(
                "goal.state created_after/created_before require view=occurrences."
            )
        if created_after and created_before and created_after > created_before:
            raise SchemaMismatch("goal.state occurrence time window is inverted.")
        limit = _positive_int(args.get("limit"), default=20, maximum=200)
        service = LoopControlService(self.home)
        try:
            if goal_id or loop_run_id:
                _require_goal_scope(
                    service,
                    goal_id=goal_id,
                    loop_run_id=loop_run_id,
                    context=context,
                )
                facts = service.goal_state(
                    goal_id=goal_id,
                    loop_run_id=loop_run_id,
                    limit=limit,
                )
                resolved_goal = facts.get("goal")
                resolved_goal_id = (
                    str(resolved_goal.get("id") or "")
                    if isinstance(resolved_goal, dict)
                    else ""
                )
                goal = service.goals.get(resolved_goal_id) if resolved_goal_id else None
                run = service.runs.get(goal.run_id) if goal and goal.run_id else None
                if loop_run_id:
                    loop_run = service.loop_runs.get_run(loop_run_id)
                else:
                    loop_runs = (
                        service.loop_runs.list_by_goal(resolved_goal_id, limit=1)
                        if resolved_goal_id
                        else []
                    )
                    loop_run = loop_runs[0] if loop_runs else None
                facts = {
                    **facts,
                    "run_diagnostics": _run_diagnostics(run),
                    "loop_diagnostics": _loop_run_diagnostics(loop_run),
                }
            else:
                if parent_goal_id:
                    _require_goal_scope(
                        service,
                        goal_id=parent_goal_id,
                        loop_run_id="",
                        context=context,
                    )
                facts = _scoped_goal_state(
                    service,
                    context=context,
                    limit=limit,
                    view=view,
                    parent_goal_id=parent_goal_id,
                    created_after=created_after,
                    created_before=created_before,
                )
        except KeyError as exc:
            raise NotFound(str(exc)) from exc
        facts = {
            **facts,
            "evidence_contract": _goal_state_evidence_contract(facts),
        }
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


def _optional_string_tuple(args: dict[str, Any], key: str) -> tuple[str, ...] | None:
    if key not in args:
        return None
    return _string_tuple(args.get(key))


def _nonnegative_int(value: Any, *, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(parsed, maximum))


def _optional_nonnegative_int(
    args: dict[str, Any],
    key: str,
    *,
    maximum: int,
    default: int,
) -> int:
    if key not in args:
        return default
    return _nonnegative_int(args.get(key), maximum=maximum)


def _nonnegative_float(value: Any, *, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(parsed, maximum))


def _optional_nonnegative_float(
    args: dict[str, Any],
    key: str,
    *,
    maximum: float,
    default: float,
) -> float:
    if key not in args:
        return default
    return _nonnegative_float(args.get(key), maximum=maximum)


def _updated_allowed_capabilities(
    args: dict[str, Any],
    *,
    home: Path,
    workspace: str,
    context: CapabilityContext,
    runtime: Any | None,
    permission_ceiling: str,
) -> tuple[str, ...] | None:
    if "allowed_capabilities" not in args:
        return None
    requested = _string_tuple(args.get("allowed_capabilities"))
    if not requested:
        raise SchemaMismatch("goal.update allowed_capabilities must be non-empty when provided.")
    return _effective_allowed_capabilities(
        requested=requested,
        context=context,
        registry=_planner_capabilities(
            home,
            workspace,
            context=context,
            runtime=runtime,
        ),
        permission_ceiling=permission_ceiling,
    )


def _effective_allowed_capabilities(
    *,
    requested: tuple[str, ...],
    context: CapabilityContext,
    registry: Any | None,
    permission_ceiling: str,
) -> tuple[str, ...]:
    if registry is None:
        if context.allowed_tools is None:
            raise SchemaMismatch("goal.open requires an explicit capability registry")
        visible = set(context.allowed_tools)
    else:
        visible = {
            spec.name
            for spec in registry.planner_specs()
            if spec.permission_policy != "static"
            or PERMISSION_ORDER[spec.permission]
            <= PERMISSION_ORDER[permission_ceiling]
        }
    if context.allowed_tools is not None:
        visible &= set(context.allowed_tools)
    if requested:
        requested_set = set(requested)
        wildcard = "*" in requested_set
        unknown = sorted(requested_set - visible - {"*"})
        if unknown:
            raise SchemaMismatch(
                "goal capabilities are outside the current policy envelope: "
                + ", ".join(unknown)
            )
        if not wildcard:
            visible &= requested_set
    if not visible:
        raise SchemaMismatch("goal.open has no capabilities in the current policy envelope")
    return tuple(sorted(visible))


def _goal_permission_ceiling(
    value: str,
    *,
    context: CapabilityContext,
) -> str:
    try:
        ceiling = normalize_permission(value, default=context.permission_ceiling)
    except ValueError as exc:
        raise SchemaMismatch(str(exc)) from exc
    if PERMISSION_ORDER[ceiling] > PERMISSION_ORDER[context.permission_ceiling]:
        raise PermissionDenied(
            "goal permission ceiling exceeds the caller policy envelope."
        )
    return ceiling


def _schedule_conflict_result(exc: ScheduleConflict) -> CapabilityResult:
    return _failure_result(
        "error",
        str(exc),
        error_reason="conflict",
        facts=exc.to_facts(),
    )


def _require_goal_scope(
    service: LoopControlService,
    *,
    goal_id: str,
    loop_run_id: str,
    context: CapabilityContext,
) -> None:
    goal = None
    if loop_run_id:
        loop_run = service.loop_runs.get_run(loop_run_id)
        if loop_run is None:
            raise NotFound(f"loop run not found: {loop_run_id}")
        goal = service.goals.get(loop_run.goal_id)
    elif goal_id:
        goal = service.goals.get(goal_id)
    if goal is None:
        raise NotFound("goal not found")
    from ..control import run_matches_context

    if not run_matches_context(goal, context):
        raise PermissionDenied("goal identity does not match caller.")
    if context.workspace and goal.workspace and not workspaces_match(
        service.home,
        goal.workspace,
        context.workspace,
    ):
        raise PermissionDenied("goal workspace does not match caller.")


def _goal_control_preflight(
    home: Path,
    args: dict[str, Any],
    *,
    context: CapabilityContext,
    operation: str,
) -> CapabilityResult | None:
    """Authorize goal control selectors before creating an approval request.

    The same checks run again inside the mutating handler to protect against
    state changes between approval and execution.
    """

    service = LoopControlService(home)
    goal_id = _arg_text(args, "goal_id")
    loop_run_id = _arg_text(args, "loop_run_id")
    goal_ids = _string_tuple(args.get("goal_ids")) if operation == "cancel" else ()
    try:
        if not goal_id and not loop_run_id and not goal_ids:
            raise SchemaMismatch(
                f"goal.{operation} requires goal_id"
                + (", loop_run_id, or explicit goal_ids." if operation == "cancel" else " or loop_run_id.")
            )
        if goal_ids:
            if goal_id or loop_run_id:
                raise SchemaMismatch(
                    "goal.cancel batch selectors cannot be combined with goal_id or loop_run_id."
                )
            for selected_goal_id in goal_ids:
                _require_goal_scope(
                    service,
                    goal_id=selected_goal_id,
                    loop_run_id="",
                    context=context,
                )
        else:
            _require_goal_scope(
                service,
                goal_id=goal_id,
                loop_run_id=loop_run_id,
                context=context,
            )
    except NaviError as exc:
        return _failure_result(
            "goal",
            str(exc),
            error_reason=exc.reason,
            terminal=exc.terminal,
        )
    return None


def _scoped_goal_state(
    service: LoopControlService,
    *,
    context: CapabilityContext,
    limit: int,
    view: str,
    parent_goal_id: str = "",
    created_after: float = 0.0,
    created_before: float = 0.0,
) -> dict[str, Any]:
    scoped_goals = _goals_for_view(
        service,
        context=context,
        view=view,
        limit=limit,
        parent_goal_id=parent_goal_id,
        created_after=created_after,
        created_before=created_before,
    )
    goal_rows = []
    for goal in scoped_goals:
        d = asdict(goal)
        d.pop("evidence_json", None)
        if goal.cron_schedule:
            d["recent_occurrences"] = [
                _goal_occurrence_summary(service, child)
                for child in service.goals.list_children(goal.id, limit=3, newest=True)
            ]
            d["recent_failed_occurrences"] = [
                _goal_occurrence_summary(service, child)
                for child in service.goals.list_children(
                    goal.id,
                    limit=3,
                    newest=True,
                    resolutions=(Resolution.FAILED,),
                )
            ]
        goal_rows.append(d)
    pending_approval_goals = [
        goal
        for goal in goal_rows
        if goal.get("governance") == Governance.AWAITING_APPROVAL
        or goal.get("task_status") == "pending"
    ]
    scheduled_goals = [goal for goal in goal_rows if str(goal.get("cron_schedule") or "")]
    current_goals = [
        goal
        for goal in goal_rows
        if goal.get("phase") != Phase.ENDED and not str(goal.get("cron_schedule") or "")
    ]
    occurrence_goals = (
        [_goal_occurrence_summary(service, goal) for goal in scoped_goals]
        if view == "occurrences"
        else [
            occurrence
            for goal in goal_rows
            for occurrence in goal.get("recent_occurrences", [])
        ]
    )
    active_loop_runs = []
    for loop_run in service.loop_runs.list_current_for_goals(
        [goal.id for goal in scoped_goals],
        limit=limit,
    ):
        d = loop_run.to_dict()
        d.pop("evidence_json", None)
        active_loop_runs.append(d)
    facts = {
        "entity_type": "goal",
        "entity_id": "",
        "state_transition": "state_read",
        "turn_scope": (
            "actor"
            if view in {"scheduled", "occurrences", "history", "inbox"}
            else "current"
        ),
        "query_scope": _goal_view_query_scope(view),
        "view": view,
        "authoritative_for": _goal_view_authority(view),
        "matched_count": len(goal_rows),
        "goal_counts": _goal_counts(service, context=context),
        "goals": goal_rows,
        "current_goals": current_goals,
        "scheduled_goals": scheduled_goals,
        "occurrence_goals": occurrence_goals,
        "pending_approval_goals": pending_approval_goals,
        "history_goals": goal_rows if view == "history" else [],
        "active_loop_runs": active_loop_runs,
    }
    return facts


def _goal_state_evidence_contract(facts: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": str(facts.get("query_scope") or "navi_goal_control_plane"),
        "authority": "navi_persistent_goal_stores",
        "establishes": [
            "navi_goal_lifecycle_state",
            "navi_loop_run_state",
            "navi_scheduled_occurrence_state",
            "navi_approval_state",
            "navi_delivery_projection",
        ],
        "does_not_establish": [
            "external_application_state",
            "external_agent_task_activity",
            "external_agent_approval_state",
            "host_process_state",
        ],
    }


def _goals_for_view(
    service: LoopControlService,
    *,
    context: CapabilityContext,
    view: str,
    limit: int,
    parent_goal_id: str = "",
    created_after: float = 0.0,
    created_before: float = 0.0,
) -> list[Any]:
    if view == "scheduled":
        return service.goals.list_scoped(
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            workspace=context.workspace,
            phases=(Phase.PENDING, Phase.RUNNING, Phase.PAUSED),
            cron=True,
            limit=limit,
        )
    if view == "occurrences":
        if parent_goal_id:
            return [
                goal
                for goal in service.goals.list_children(
                    parent_goal_id,
                    limit=limit,
                    newest=True,
                    created_after=created_after,
                    created_before=created_before,
                )
                if _goal_matches_context(goal, context)
            ]
        goals = service.goals.list_scoped(
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            workspace=context.workspace,
            child=True,
            limit=200,
        )
        filtered = [
            goal
            for goal in goals
            if (not created_after or goal.created_at >= created_after)
            and (not created_before or goal.created_at <= created_before)
        ]
        return filtered[:limit]
    if view == "inbox":
        return service.goals.list_scoped(
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            workspace=context.workspace,
            phases=(Phase.PENDING, Phase.RUNNING, Phase.PAUSED),
            child=False,
            limit=limit,
        )
    if view == "pending_approval":
        return service.goals.list_scoped(
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            workspace=context.workspace,
            phases=(Phase.PENDING, Phase.RUNNING, Phase.PAUSED),
            governance=(Governance.AWAITING_APPROVAL,),
            cron=False,
            limit=limit,
        )
    if view == "history":
        return service.goals.list_scoped(
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            workspace=context.workspace,
            child=False,
            limit=limit,
        )
    return service.goals.list_scoped(
        source=context.source,
        peer_id=context.peer_id,
        sender_id=context.sender_id,
        workspace=context.workspace,
        phases=(Phase.PENDING, Phase.RUNNING, Phase.PAUSED),
        governance=(Governance.APPROVED, Governance.NONE),
        cron=False,
        child=False,
        limit=limit,
    )


def _goal_view_authority(view: str) -> str:
    return {
        "current": "current_actor_foreground_goals",
        "inbox": "current_actor_task_inbox",
        "scheduled": "actor_scheduled_goals",
        "occurrences": "actor_scheduled_occurrences",
        "pending_approval": "current_actor_pending_approval_goals",
        "history": "actor_goal_history",
    }[view]


def _goal_matches_context(goal: Any, context: CapabilityContext) -> bool:
    return all(
        not expected or str(actual) == str(expected)
        for actual, expected in (
            (goal.source, context.source),
            (goal.peer_id, context.peer_id),
            (goal.sender_id, context.sender_id),
            (goal.workspace, context.workspace),
        )
    )


def _goal_occurrence_summary(service: LoopControlService, goal: Any) -> dict[str, Any]:
    accepted = service.goals.accepted_result_for_run(goal.run_id) if goal.run_id else {}
    delivery = {
        key: value
        for key, value in accepted.items()
        if key not in {"body", "body_provenance"}
    }
    run = service.runs.get(goal.run_id) if goal.run_id else None
    loop_runs = service.loop_runs.list_by_goal(goal.id, limit=1)
    loop_run = loop_runs[0] if loop_runs else None
    return {
        "goal_id": goal.id,
        "parent_goal_id": goal.parent_goal_id,
        "run_id": goal.run_id,
        "trace_id": goal.trace_id or goal.run_id,
        "phase": goal.phase,
        "acceptance": goal.acceptance,
        "resolution": goal.resolution,
        "task_status": goal.task_status,
        "blocked_reason": goal.blocked_reason,
        "created_at": goal.created_at,
        "updated_at": goal.updated_at,
        "delivery": delivery,
        "run_diagnostics": _run_diagnostics(run),
        "loop_diagnostics": _loop_run_diagnostics(loop_run),
    }


def _run_diagnostics(run: Any | None) -> dict[str, Any]:
    return {
        "phase": str(run.phase) if run is not None else "",
        "governance": str(run.governance) if run is not None else "",
        "acceptance": str(run.acceptance) if run is not None else "",
        "resolution": str(run.resolution) if run is not None else "",
        "error": str(run.error) if run is not None else "",
    }


def _loop_run_diagnostics(loop_run: Any | None) -> dict[str, Any]:
    if loop_run is None:
        return {}
    evidence = loop_run.evidence if isinstance(loop_run.evidence, dict) else {}
    raw_args = evidence.get("args")
    args = dict(raw_args) if isinstance(raw_args, dict) else {}
    checker_summaries: list[str] = []
    raw_checker_results = evidence.get("checker_results")
    if isinstance(raw_checker_results, list):
        for item in raw_checker_results[-3:]:
            if not isinstance(item, dict):
                continue
            raw_checker_evidence = item.get("evidence")
            checker_evidence = (
                raw_checker_evidence if isinstance(raw_checker_evidence, dict) else {}
            )
            summary = str(checker_evidence.get("evidence_summary") or "").strip()
            if summary:
                checker_summaries.append(summary[:1200])
    return {
        "loop_run_id": str(loop_run.run_id),
        "node": str(loop_run.node),
        "terminal_state": str(loop_run.terminal_state),
        "attempt": int(loop_run.attempt),
        "reason_code": str(evidence.get("reason_code") or ""),
        "reason": str(evidence.get("reason") or ""),
        "error_type": str(args.get("error_type") or ""),
        "checker_summaries": checker_summaries,
        "diagnostic_authority": "persisted_loop_run_state",
    }


def _goal_view_query_scope(view: str) -> str:
    return (
        "actor_workspace"
        if view in {"inbox", "current", "pending_approval", "history"}
        else "actor"
    )


def _goal_counts(
    service: LoopControlService,
    *,
    context: CapabilityContext,
) -> dict[str, int]:
    active_phases = (Phase.PENDING, Phase.RUNNING, Phase.PAUSED)
    return {
        "current": service.goals.count_scoped(
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            workspace=context.workspace,
            phases=active_phases,
            governance=(Governance.APPROVED, Governance.NONE),
            cron=False,
            child=False,
        ),
        "scheduled": service.goals.count_scoped(
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            workspace=context.workspace,
            phases=active_phases,
            cron=True,
        ),
        "pending_approval": service.goals.count_scoped(
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            workspace=context.workspace,
            phases=active_phases,
            governance=(Governance.AWAITING_APPROVAL,),
            cron=False,
        ),
        "history": service.goals.count_scoped(
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            workspace=context.workspace,
            child=False,
        ),
    }


def _cancel_goal_batch(
    service: LoopControlService,
    *,
    goals: list[Any],
    context: CapabilityContext,
    reason: str,
    selector: dict[str, Any],
) -> CapabilityResult:
    cancelled: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for goal in goals:
        try:
            _require_goal_scope(
                service,
                goal_id=goal.id,
                loop_run_id="",
                context=context,
            )
            if context.workspace and goal.workspace and not workspaces_match(
                service.home,
                goal.workspace,
                context.workspace,
            ):
                raise PermissionDenied("goal workspace does not match caller.")
            result = service.cancel_goal(
                goal_id=goal.id,
                reason=reason or "batch_cancel:goal_ids",
            )
            facts = _with_verified_goal(service, result.to_facts(), result.goal.id)
            item = {
                "goal_id": result.goal.id,
                "objective": result.goal.objective,
                "state_transition": facts.get("state_transition"),
                "verified_goal": facts.get("verified_goal", {}),
            }
            if _cancel_result_verified(item):
                cancelled.append(item)
            else:
                failed.append(
                    {
                        **item,
                        "error_reason": "verification_failed",
                        "message": "goal remained in selected lifecycle view after cancellation",
                    }
                )
        except Exception as exc:  # noqa: BLE001 - batch result must report per-target facts.
            failed.append(
                {
                    "goal_id": getattr(goal, "id", ""),
                    "objective": getattr(goal, "objective", ""),
                    "error_reason": type(exc).__name__,
                    "message": str(exc),
                }
            )
    facts = {
        "entity_type": "goal_collection",
        "entity_id": "",
        "state_transition": "batch_cancelled",
        "turn_scope": "current",
        "selector": selector,
        "requested_count": len(goals),
        "cancelled_count": len(cancelled),
        "failed_count": len(failed),
        "cancelled_goals": cancelled,
        "failed_goals": failed,
        "verified_after": {
            "cancelled_goal_ids": [item["goal_id"] for item in cancelled],
            "failed_goal_ids": [item["goal_id"] for item in failed],
        },
        "completion_evidence": len(failed) == 0,
    }
    return CapabilityResult(
        ok=not failed,
        action="goal",
        facts=facts,
        error_reason="partial_batch_failure" if failed else "",
    )


def _cancel_result_verified(item: dict[str, Any]) -> bool:
    verified = item.get("verified_goal")
    if not isinstance(verified, dict):
        return False
    return verified.get("phase") == Phase.ENDED and verified.get("resolution") == Resolution.CANCELED


def _with_verified_goal(
    service: LoopControlService,
    facts: dict[str, Any],
    goal_id: str,
) -> dict[str, Any]:
    verified = service.goals.get(goal_id)
    if verified is None:
        return facts
    verified_goal = asdict(verified)
    return {
        **facts,
        "verified_goal": verified_goal,
        "verified_state": {
            "goal_id": verified.id,
            "phase": verified.phase,
            "governance": verified.governance,
            "acceptance": verified.acceptance,
            "resolution": verified.resolution,
            "task_status": verified.task_status,
            "cron_schedule": verified.cron_schedule,
        },
        "verification_evidence": True,
    }


def _goal_result(
    *,
    result: Any,
    facts: dict[str, Any],
    action: str,
    message: str,
) -> CapabilityResult:
    terminal_state = str(result.loop_run.terminal_state or "")
    failed_states = {
        str(LoopTerminalState.BLOCKED),
        str(LoopTerminalState.FAILED),
        str(LoopTerminalState.TIMED_OUT),
        str(LoopTerminalState.CONFLICTED),
        str(LoopTerminalState.CANCELLED),
        str(LoopTerminalState.SUPERSEDED),
    }
    paused_states = {
        str(LoopTerminalState.PAUSED),
        str(LoopTerminalState.WAITING_APPROVAL),
    }
    target_is_paused = terminal_state in paused_states
    return CapabilityResult(
        ok=terminal_state not in failed_states,
        action=action,
        message=message,
        run_id=result.run.id,
        terminal=bool(terminal_state) and not target_is_paused,
        facts=facts,
        error_reason=f"loop_{terminal_state}" if terminal_state in failed_states else "",
        # The returned LoopRun is the governed target of goal.open/resume, not
        # the control turn that invoked this capability. Its durable gate is
        # reported in facts and must never be copied into the control LoopRun.
        yields_control=False,
    )


def _planner_capabilities(
    home: Path,
    workspace: str,
    *,
    context: CapabilityContext,
    runtime: Any,
):
    from ..capabilities import CapabilityRegistry

    return CapabilityRegistry(
        home=home,
        project_dir=Path(workspace),
        allowed_tools=(set(context.allowed_tools) if context.allowed_tools is not None else None),
        disabled_tools=set(context.disabled_tools),
        disabled_capability_classes=context.disabled_capability_classes,
        permission_ceiling=context.permission_ceiling,
        runtime=runtime,
    )


def _promote_outbound_facts(result: Any) -> dict[str, Any]:
    """Lift a connector delivery contract to the connector response boundary.

    The planner ReAct loop executes capabilities and stores their results in
    ``state_graph_result.evidence["capability_result"]``. The active connector
    persists the structured contract through the shared durable outbox. The
    contract is connector-neutral, so Weixin, email, Feishu, or another adapter
    can implement the same boundary without a channel-specific staging area.

    Only an explicitly paused ask or a checker-accepted response is stored in
    ``evidence["responded_message"]``. Raw capability output is not a reply
    authority because it may have been rejected by verification.
    """
    state_graph_result = getattr(result, "state_graph_result", None)
    if state_graph_result is None:
        return {}
    evidence = getattr(state_graph_result, "evidence", None) or {}
    from ..connector_delivery import connector_delivery_from_loop_result

    delivery = connector_delivery_from_loop_result(result)
    if delivery is not None:
        return {
            "action": "connector_outbound",
            "connector_delivery": delivery.to_dict(),
            "responded_message": delivery.text,
            "responded_action": "connector_outbound",
        }
    responded_message = str(evidence.get("responded_message") or "")
    if responded_message:
        return {
            "responded_message": responded_message,
            "responded_action": str(evidence.get("responded_action") or ""),
        }
    capability_result = evidence.get("capability_result")
    if not isinstance(capability_result, dict):
        return {}
    action = str(capability_result.get("action") or "")
    cap_facts = capability_result.get("facts")
    if action == "approval" and isinstance(cap_facts, dict):
        pending = (
            cap_facts.get("pending_approval")
            or cap_facts.get("current_approval")
            or cap_facts.get("approval")
        )
        if isinstance(pending, dict):
            return {"pending_approval": dict(pending)}
    return {}
