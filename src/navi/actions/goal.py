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
            loop_kind=_arg_text(args, "loop_kind") or "durable_goal",
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
                opened = service.open_goal(replace(request, auto_start=False))
                result = await run_goal_loop_state_graph(
                    home=self.home,
                    service=service,
                    base=opened,
                    runtime=self.runtime,
                    planner_capabilities=_planner_capabilities(
                        self.home,
                        opened.goal.workspace,
                        context=context,
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
        facts = result.to_facts()
        # Promote connector_outbound side effects (e.g. a staged file the
        # weixin connector should actually send) to the top level so the
        # connector runtime's _send_reply can dispatch them. Without this,
        # the file sits in the outbox forever and the user gets nothing.
        promoted = _promote_outbound_facts(result)
        if promoted:
            facts = {**facts, **promoted}
        responded_message = str(facts.get("responded_message") or "")
        return CapabilityResult(
            ok=True,
            action="goal",
            message=responded_message,
            run_id=result.run.id,
            facts=facts,
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
    """Lift connector_outbound side effects to the top level of the facts.

    The planner ReAct loop executes capabilities and stores their results in
    ``state_graph_result.evidence["capability_result"]``. When the last
    capability was a connector outbound (e.g. weixin send_file), its
    ``outbound_path`` and ``action`` must be promoted so the connector
    runtime's ``_send_reply`` can actually dispatch the file. Without this
    promotion the file is staged to the outbox but never sent, and the user
    receives nothing.

    A ``respond`` capability's message is preserved in
    ``evidence["responded_message"]`` across the whole loop (even when a
    later capability overwrites ``capability_result``), so we promote it
    here too — otherwise the question the model asked never reaches the user.
    """
    state_graph_result = getattr(result, "state_graph_result", None)
    if state_graph_result is None:
        return {}
    evidence = getattr(state_graph_result, "evidence", None) or {}
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
    if not isinstance(cap_facts, dict):
        return {}
    if action == "chat" or action == "ask":
        message = str(capability_result.get("message") or "")
        if not message:
            return {}
        return {"responded_message": message, "responded_action": action}
    outbound_path = str(cap_facts.get("outbound_path") or "")
    if action != "connector_outbound" or not outbound_path:
        return {}
    promoted = {
        "action": action,
        "outbound_path": outbound_path,
        "outbound_message": str(capability_result.get("message") or ""),
    }
    for key in (
        "entity_type",
        "entity_id",
        "state_transition",
        "side_effect_scope",
        "side_effect_state",
        "side_effect_artifact",
        "side_effect_commit",
        "side_effect_compensate",
        "side_effect_commit_strategy",
    ):
        if key in cap_facts:
            promoted[key] = cap_facts[key]
    return promoted
