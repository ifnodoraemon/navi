from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ..capabilities_types import BaseCapability, CapabilityContext, CapabilityResult, capability
from ..goal_state_graph import run_goal_loop_state_graph
from ..loop_control_service import LoopControlService, OpenGoalRequest
from ..loop_contracts import LoopTerminalState
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
        planner_capabilities = _planner_capabilities(
            self.home,
            workspace,
            context=context,
            runtime=self.runtime,
        )
        requested_capabilities = _string_tuple(args.get("allowed_capabilities"))
        allowed_capabilities = _effective_allowed_capabilities(
            requested=requested_capabilities,
            context=context,
            registry=planner_capabilities,
        )
        request = OpenGoalRequest(
            objective=objective,
            workspace=workspace,
            loop_kind=_arg_text(args, "loop_kind") or "durable_goal",
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            session_id=context.session_id or "",
            scope=_string_tuple(args.get("scope")),
            constraints=_string_tuple(args.get("constraints")),
            acceptance_criteria=_string_tuple(args.get("acceptance_criteria")),
            permission_ceiling=_arg_text(args, "permission_ceiling") or context.permission_ceiling,
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
        )
        try:
            service = LoopControlService(self.home)
            if (
                request.auto_start
                and self.runtime is not None
                and request.loop_kind != "scheduled"
            ):
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
        except ValueError as exc:
            raise SchemaMismatch(str(exc)) from exc
        facts = result.to_facts()
        # Promote connector delivery contracts to the response boundary so the
        # active adapter can execute them synchronously.
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


def _nonnegative_int(value: Any, *, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(parsed, maximum))


def _nonnegative_float(value: Any, *, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(parsed, maximum))


def _effective_allowed_capabilities(
    *,
    requested: tuple[str, ...],
    context: CapabilityContext,
    registry: Any | None,
) -> tuple[str, ...]:
    if registry is None:
        if context.allowed_tools is None:
            raise SchemaMismatch("goal.open requires an explicit capability registry")
        visible = set(context.allowed_tools)
    else:
        visible = {
            spec.name
            for spec in registry.planner_specs(
                permission_ceiling=context.permission_ceiling,
            )
        }
    if context.allowed_tools is not None:
        visible &= set(context.allowed_tools)
    if requested:
        unknown = sorted(set(requested) - visible)
        if unknown:
            raise SchemaMismatch(
                "goal.open capabilities are outside the current policy envelope: "
                + ", ".join(unknown)
            )
        visible &= set(requested)
    if not visible:
        raise SchemaMismatch("goal.open has no capabilities in the current policy envelope")
    return tuple(sorted(visible))


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
    return CapabilityResult(
        ok=terminal_state not in failed_states,
        action=action,
        message=message,
        run_id=result.run.id,
        terminal=bool(terminal_state) and terminal_state not in paused_states,
        facts=facts,
        error_reason=f"loop_{terminal_state}" if terminal_state in failed_states else "",
        yields_control=terminal_state in paused_states,
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
        allowed_tools=(
            set(context.allowed_tools) if context.allowed_tools is not None else None
        ),
        disabled_tools=set(context.disabled_tools),
        disabled_capability_classes=context.disabled_capability_classes,
        permission_ceiling=context.permission_ceiling,
        runtime=runtime,
    )


def _promote_outbound_facts(result: Any) -> dict[str, Any]:
    """Lift a connector delivery contract to the connector response boundary.

    The planner ReAct loop executes capabilities and stores their results in
    ``state_graph_result.evidence["capability_result"]``. When the last
    The active connector consumes the structured contract synchronously.  The
    contract is connector-neutral so Weixin, email, Feishu, or another adapter
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
