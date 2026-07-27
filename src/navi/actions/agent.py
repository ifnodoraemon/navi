from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..capabilities_types import (
    BaseCapability,
    CapabilityContext,
    CapabilityResult,
    capability,
)
from ..goals import ChildAdmissionConflict, Goal, GoalStore
from ..lifecycle import Phase
from ..loop_control_service import LoopControlService, OpenGoalRequest
from ..loop_contracts import LoopSpec, LoopTerminalState
from ..operating_context import permission_allows
from ..result import Conflict, NotFound, PermissionDenied, SchemaMismatch, guarded
from ..runs import RunStore
from ..tools import ToolSpec
from .helpers import arg_text as _arg_text, fact_result as _fact_result


MAX_ACTIVE_CHILDREN = 3
MAX_CHILD_TIMEOUT_SECONDS = 900
MAX_CHILD_TOKEN_BUDGET = 50_000
MAX_CHILD_CALL_BUDGET = 12
MAX_CHILD_COST_BUDGET = 2.0
MAX_CHILD_QPS = 5

_CHILD_WORK_PERMISSION_CEILING = "network"
_CHILD_REPORT_CAPABILITY = "agent.report"
_BLOCKED_CHILD_CLASSES = {
    "agent",
    "approval",
    "connector",
    "conversation",
    "evolution",
    "goal",
    "session",
}


class AgentSpawnCapability(BaseCapability):
    def __init__(
        self,
        spec: ToolSpec,
        *,
        home: Path,
        project_dir: Path,
        capability_registry: Any | None = None,
    ):
        super().__init__(spec, home=home)
        self.project_dir = project_dir
        self.capability_registry = capability_registry

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
            raise SchemaMismatch("agent.control(operation=spawn) requires objective.")
        acceptance_criteria = _string_tuple(args.get("acceptance_criteria"))
        if not acceptance_criteria:
            raise SchemaMismatch("agent.control(operation=spawn) requires acceptance_criteria.")
        requested = _string_tuple(args.get("allowed_capabilities"))
        if not requested:
            raise SchemaMismatch(
                "agent.control(operation=spawn) requires explicit allowed_capabilities."
            )
        store = GoalStore(self.home)
        parent = _parent_for_actor(store, args=args, context=context)
        active_child_count = store.count_children(
            parent.id,
            phases=(Phase.PENDING, Phase.RUNNING, Phase.PAUSED),
        )
        if active_child_count >= MAX_ACTIVE_CHILDREN:
            raise Conflict(
                "agent.control(operation=spawn) allows at most "
                f"{MAX_ACTIVE_CHILDREN} active children per parent."
            )

        service = LoopControlService(self.home)
        parent_spec = service.goal_loop_spec(parent.id)
        allowed_capabilities = _child_capability_envelope(
            requested=requested,
            parent_spec=parent_spec,
            registry=self.capability_registry,
            context=context,
        )
        if not permission_allows("prepare", parent_spec.goal.permission_ceiling):
            raise PermissionDenied(
                "Parent goal permission ceiling does not authorize child-agent lifecycle state."
            )
        child_ceiling = "prepare"

        timeout_seconds = _bounded_positive_int(
            args.get("timeout_seconds"),
            default=600,
            upper=min(
                MAX_CHILD_TIMEOUT_SECONDS,
                _parent_remaining_timeout_seconds(parent),
            ),
        )
        token_budget = _intersect_budget(
            args.get("token_budget"),
            default=20_000,
            system_limit=MAX_CHILD_TOKEN_BUDGET,
            parent_limit=parent_spec.budget_policy.token_budget,
        )
        call_budget = _intersect_budget(
            args.get("call_budget"),
            default=8,
            system_limit=MAX_CHILD_CALL_BUDGET,
            parent_limit=parent_spec.budget_policy.call_budget,
        )
        cost_budget = _intersect_float_budget(
            args.get("cost_budget"),
            default=1.0,
            system_limit=MAX_CHILD_COST_BUDGET,
            parent_limit=parent_spec.budget_policy.cost_budget,
        )
        qps_limit = _intersect_budget(
            args.get("qps_limit"),
            default=MAX_CHILD_QPS,
            system_limit=MAX_CHILD_QPS,
            parent_limit=parent_spec.budget_policy.qps_limit,
        )
        context_facts = args.get("context_facts")
        if context_facts is not None and not isinstance(context_facts, dict):
            raise SchemaMismatch("agent.control(operation=spawn) context_facts must be an object.")

        request = OpenGoalRequest(
            objective=objective,
            workspace=parent.workspace,
            source=parent.source,
            peer_id=parent.peer_id,
            sender_id=parent.sender_id,
            session_id=parent.session_id,
            scope=(f"delegated-by:{parent.id}", f"repo:{parent.workspace}"),
            constraints=(
                "Return findings to the parent only through agent.report.",
                "Do not contact the user or request user input.",
                "Do not spawn, control, or message other agents.",
                "Do not execute mutating, approval, connector, or response capabilities.",
            ),
            acceptance_criteria=acceptance_criteria,
            permission_ceiling=child_ceiling,
            allowed_capabilities=allowed_capabilities,
            timeout_seconds=timeout_seconds,
            token_budget=token_budget,
            call_budget=call_budget,
            cost_budget=cost_budget,
            qps_limit=qps_limit,
            max_concurrent=1,
            auto_start=False,
            execution_mode="background",
            parent_goal_id=parent.id,
            child_active_limit=MAX_ACTIVE_CHILDREN,
            trigger_facts={
                "type": "agent_delegation",
                "parent_goal_id": parent.id,
                "context_facts": dict(context_facts or {}),
            },
        )
        try:
            opened = service.open_goal(request)
        except ChildAdmissionConflict as exc:
            raise Conflict(str(exc)) from exc
        facts = {
            "entity_type": "agent",
            "entity_id": opened.goal.id,
            "state_transition": "spawned",
            "turn_scope": "current",
            "parent_goal_id": parent.id,
            "child_goal_id": opened.goal.id,
            "run_id": opened.run.id,
            "loop_run_id": opened.loop_run.run_id,
            "phase": opened.goal.phase,
            "task_status": opened.goal.task_status,
            "execution_mode": "background",
            "permission_ceiling": child_ceiling,
            "allowed_capabilities": list(allowed_capabilities),
            "budget_policy": opened.loop_spec.budget_policy.to_dict(),
            "timeout_seconds": timeout_seconds,
            "active_children": active_child_count + 1,
            "max_active_children": MAX_ACTIVE_CHILDREN,
        }
        return _fact_result("agent", facts, run_id=opened.run.id)


class AgentListCapability(BaseCapability):
    def __init__(self, spec: ToolSpec, *, home: Path):
        super().__init__(spec, home=home)

    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        store = GoalStore(self.home)
        parent = _parent_for_actor(store, args=args, context=context)
        limit = _bounded_positive_int(args.get("limit"), default=20, upper=100)
        all_children = store.list_children(parent.id, limit=1000, newest=True)
        children = all_children[:limit]
        facts = {
            "entity_type": "agent",
            "entity_id": parent.id,
            "state_transition": "listed",
            "turn_scope": "current",
            "parent_goal_id": parent.id,
            "children": [_child_state(self.home, child) for child in children],
            "active_children": sum(child.phase != Phase.ENDED for child in all_children),
            "max_active_children": MAX_ACTIVE_CHILDREN,
        }
        return _fact_result("agent", facts, run_id=parent.run_id)


class AgentStateCapability(BaseCapability):
    def __init__(self, spec: ToolSpec, *, home: Path):
        super().__init__(spec, home=home)

    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        store = GoalStore(self.home)
        parent = _parent_for_actor(store, args=args, context=context)
        child = _child_for_parent(store, parent=parent, args=args)
        facts = {
            "entity_type": "agent",
            "entity_id": child.id,
            "state_transition": "state_read",
            "turn_scope": "current",
            "parent_goal_id": parent.id,
            "child": _child_state(self.home, child),
        }
        return _fact_result("agent", facts, run_id=child.run_id)


class AgentMessageCapability(BaseCapability):
    def __init__(self, spec: ToolSpec, *, home: Path):
        super().__init__(spec, home=home)

    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        store = GoalStore(self.home)
        parent = _parent_for_actor(store, args=args, context=context)
        child = _child_for_parent(store, parent=parent, args=args)
        if child.phase == Phase.ENDED:
            raise Conflict("agent.control(operation=message) cannot update a terminal child.")
        message = _arg_text(args, "message")
        supplied_facts = args.get("facts")
        if supplied_facts is not None and not isinstance(supplied_facts, dict):
            raise SchemaMismatch("agent.control(operation=message) facts must be an object.")
        if not message and not supplied_facts:
            raise SchemaMismatch("agent.control(operation=message) requires message or facts.")
        evidence = {
            "state_transition": "message_received",
            "from_parent_goal_id": parent.id,
            "message": message,
            "facts": dict(supplied_facts or {}),
        }
        received = store.record_event(
            child.id,
            "agent.message_received",
            phase=child.phase,
            governance=child.governance,
            acceptance=child.acceptance,
            resolution=child.resolution,
            run_id=child.run_id,
            trace_id=child.trace_id,
            evidence=evidence,
        )
        store.record_event(
            parent.id,
            "agent.message_sent",
            phase=parent.phase,
            governance=parent.governance,
            acceptance=parent.acceptance,
            resolution=parent.resolution,
            run_id=child.run_id,
            trace_id=parent.trace_id,
            evidence={**evidence, "child_goal_id": child.id, "message_id": received.id},
        )
        return _fact_result(
            "agent",
            {
                "entity_type": "agent",
                "entity_id": child.id,
                "state_transition": "message_sent",
                "turn_scope": "current",
                "parent_goal_id": parent.id,
                "child_goal_id": child.id,
                "message_id": received.id,
            },
            run_id=child.run_id,
        )


class AgentCancelCapability(BaseCapability):
    def __init__(self, spec: ToolSpec, *, home: Path):
        super().__init__(spec, home=home)

    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        store = GoalStore(self.home)
        parent = _parent_for_actor(store, args=args, context=context)
        child = _child_for_parent(store, parent=parent, args=args)
        try:
            result = LoopControlService(self.home).cancel_goal(
                goal_id=child.id,
                reason=_arg_text(args, "reason") or "parent_cancel_requested",
            )
        except ValueError as exc:
            raise Conflict(str(exc)) from exc
        facts = {
            **result.to_facts(),
            "entity_type": "agent",
            "entity_id": child.id,
            "parent_goal_id": parent.id,
            "child_goal_id": child.id,
        }
        return _fact_result("agent", facts, run_id=child.run_id)


class AgentCollectCapability(BaseCapability):
    def __init__(self, spec: ToolSpec, *, home: Path):
        super().__init__(spec, home=home)

    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        store = GoalStore(self.home)
        parent = _parent_for_actor(store, args=args, context=context)
        child = _child_for_parent(store, parent=parent, args=args)
        state = _child_state(self.home, child)
        reports = _agent_reports(store, child.id)
        facts = {
            "entity_type": "agent",
            "entity_id": child.id,
            "state_transition": "collected",
            "turn_scope": "current",
            "parent_goal_id": parent.id,
            "child_goal_id": child.id,
            "completion_evidence": state["completion_evidence"],
            "child": state,
            "reports": reports,
            "latest_report": reports[-1] if reports else {},
        }
        return _fact_result("agent", facts, run_id=child.run_id)


@capability("agent_control")
class AgentControlCapability(BaseCapability):
    """One parent-side lifecycle surface whose operation declares the effect."""

    def __init__(
        self,
        spec: ToolSpec,
        *,
        home: Path,
        project_dir: Path,
        capability_registry: Any | None = None,
    ):
        super().__init__(spec, home=home)
        self.project_dir = project_dir
        self.capability_registry = capability_registry

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        operation = _arg_text(args, "operation").lower()
        handlers: dict[str, BaseCapability] = {
            "spawn": AgentSpawnCapability(
                self.spec,
                home=self.home,
                project_dir=self.project_dir,
                capability_registry=self.capability_registry,
            ),
            "list": AgentListCapability(self.spec, home=self.home),
            "state": AgentStateCapability(self.spec, home=self.home),
            "message": AgentMessageCapability(self.spec, home=self.home),
            "cancel": AgentCancelCapability(self.spec, home=self.home),
            "collect": AgentCollectCapability(self.spec, home=self.home),
        }
        handler = handlers.get(operation)
        if handler is None:
            return CapabilityResult(
                ok=False,
                action="agent_control",
                message=(
                    "agent.control operation must be spawn, list, state, message, "
                    "cancel, or collect."
                ),
                facts={"operation": operation, "error_reason": "schema_mismatch"},
                error_reason="schema_mismatch",
            )
        return await handler.invoke(args, permission=permission, context=context)


@capability("agent_report")
class AgentReportCapability(BaseCapability):
    def __init__(self, spec: ToolSpec, *, home: Path):
        super().__init__(spec, home=home)

    @guarded
    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        if not context.goal_id:
            raise PermissionDenied("agent.report requires a governed child goal context.")
        store = GoalStore(self.home)
        child = store.get(context.goal_id)
        if child is None or not child.parent_goal_id:
            raise PermissionDenied("agent.report is available only inside a child agent.")
        if context.loop_run_id:
            loop_runs = LoopControlService(self.home).loop_runs.list_by_goal(child.id, limit=1)
            if not loop_runs or loop_runs[0].run_id != context.loop_run_id:
                raise PermissionDenied("agent.report loop identity does not match the child goal.")
        summary = _arg_text(args, "summary")
        if not summary:
            raise SchemaMismatch("agent.report requires summary.")
        report = {
            "summary": summary,
            "findings": _object_list(args.get("findings"), field="findings"),
            "evidence_refs": list(_string_tuple(args.get("evidence_refs"))),
            "artifacts": list(_string_tuple(args.get("artifacts"))),
            "unresolved": list(_string_tuple(args.get("unresolved"))),
            "needs_parent_input": bool(args.get("needs_parent_input", False)),
        }
        parent = store.get(child.parent_goal_id)
        if parent is None:
            raise NotFound("agent.report parent goal not found.")
        evidence = {
            "state_transition": "reported",
            "parent_goal_id": parent.id,
            "child_goal_id": child.id,
            "loop_run_id": context.loop_run_id,
            "report": report,
        }
        event = store.record_event(
            child.id,
            "agent.reported",
            phase=child.phase,
            governance=child.governance,
            acceptance=child.acceptance,
            resolution=child.resolution,
            run_id=child.run_id,
            trace_id=child.trace_id,
            evidence=evidence,
        )
        store.record_event(
            parent.id,
            "agent.report_received",
            phase=parent.phase,
            governance=parent.governance,
            acceptance=parent.acceptance,
            resolution=parent.resolution,
            run_id=child.run_id,
            trace_id=parent.trace_id,
            evidence={**evidence, "report_id": event.id},
        )
        return CapabilityResult(
            ok=True,
            action="agent_report",
            terminal=True,
            facts={
                "entity_type": "agent",
                "entity_id": child.id,
                "state_transition": "reported",
                "turn_scope": "parent",
                "parent_goal_id": parent.id,
                "child_goal_id": child.id,
                "report_id": event.id,
                "report": report,
            },
        )


def _parent_for_actor(
    store: GoalStore,
    *,
    args: dict[str, Any],
    context: CapabilityContext,
) -> Goal:
    if context.goal_id:
        actor = store.get(context.goal_id)
        if actor is not None and actor.parent_goal_id:
            raise PermissionDenied(
                "Child agents may report to their parent but cannot manage agents."
            )
    parent_goal_id = _arg_text(args, "parent_goal_id") or context.goal_id
    if not parent_goal_id:
        raise SchemaMismatch("agent capability requires parent_goal_id.")
    parent = store.get(parent_goal_id)
    if parent is None:
        raise NotFound("parent goal not found.")
    if parent.parent_goal_id:
        raise PermissionDenied("Recursive agent delegation is disabled.")
    _require_same_identity(parent, context)
    return parent


def _require_same_identity(goal: Goal, context: CapabilityContext) -> None:
    for label, expected, actual in (
        ("source", goal.source, context.source),
        ("peer", goal.peer_id, context.peer_id),
        ("sender", goal.sender_id, context.sender_id),
    ):
        if expected and actual and expected != actual:
            raise PermissionDenied(f"agent goal {label} identity does not match caller.")


def _child_for_parent(
    store: GoalStore,
    *,
    parent: Goal,
    args: dict[str, Any],
) -> Goal:
    child_goal_id = _arg_text(args, "child_goal_id")
    if not child_goal_id:
        raise SchemaMismatch("agent capability requires child_goal_id.")
    child = store.get(child_goal_id)
    if child is None or child.parent_goal_id != parent.id:
        raise NotFound("child agent not found for parent.")
    return child


def _child_capability_envelope(
    *,
    requested: tuple[str, ...],
    parent_spec: LoopSpec,
    registry: Any | None,
    context: CapabilityContext,
) -> tuple[str, ...]:
    if registry is None:
        raise SchemaMismatch(
            "agent.control(operation=spawn) requires an explicit capability registry."
        )
    parent_allowed = set(parent_spec.allowed_capabilities)
    context_allowed = set(context.allowed_tools) if context.allowed_tools is not None else None
    eligible: set[str] = set()
    for spec in registry.planner_specs(permission_ceiling=_CHILD_WORK_PERMISSION_CEILING):
        if not spec.delegation_allowed:
            continue
        if spec.capability_class in _BLOCKED_CHILD_CLASSES:
            continue
        if spec.mutates and spec.permission_policy == "static":
            continue
        if spec.permission not in {"read", "network"}:
            continue
        if "*" not in parent_allowed and spec.name not in parent_allowed:
            continue
        if context_allowed is not None and spec.name not in context_allowed:
            continue
        eligible.add(spec.name)
    outside = sorted(set(requested) - eligible)
    if outside:
        raise SchemaMismatch(
            "agent.control(operation=spawn) capabilities are outside the child "
            "policy envelope: " + ", ".join(outside)
        )
    return tuple(sorted({*requested, _CHILD_REPORT_CAPABILITY}))


def _child_state(home: Path, child: Goal) -> dict[str, Any]:
    runs = RunStore(home)
    service = LoopControlService(home)
    run = runs.get(child.run_id) if child.run_id else None
    loop_runs = service.loop_runs.list_by_goal(child.id, limit=1)
    loop_run = loop_runs[0] if loop_runs else None
    terminal_state = str(loop_run.terminal_state or "") if loop_run else ""
    completion_evidence = bool(
        child.phase == Phase.ENDED
        and terminal_state == str(LoopTerminalState.CONVERGED)
        and run is not None
        and not run.error
    )
    return {
        "child_goal_id": child.id,
        "parent_goal_id": child.parent_goal_id,
        "objective": child.objective,
        "run_id": child.run_id,
        "loop_run_id": loop_run.run_id if loop_run else "",
        "phase": child.phase,
        "governance": child.governance,
        "acceptance": child.acceptance,
        "resolution": child.resolution,
        "task_status": child.task_status,
        "loop_node": str(loop_run.node) if loop_run else "",
        "loop_terminal_state": terminal_state,
        "result_summary": str(run.result_summary or "") if run else "",
        "result_summary_provenance": "assistant_candidate_non_authoritative",
        "error": str(run.error or "") if run else "",
        "completion_evidence": completion_evidence,
        "created_at": child.created_at,
        "updated_at": child.updated_at,
    }


def _agent_reports(store: GoalStore, child_goal_id: str) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for event in store.list_events(child_goal_id, limit=None):
        if event.event_type != "agent.reported":
            continue
        try:
            evidence = json.loads(event.evidence_json or "{}")
        except json.JSONDecodeError:
            evidence = {}
        report = evidence.get("report") if isinstance(evidence, dict) else None
        if isinstance(report, dict):
            reports.append(
                {
                    "report_id": event.id,
                    "created_at": event.created_at,
                    "loop_run_id": str(evidence.get("loop_run_id") or ""),
                    **report,
                }
            )
    return reports


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, list | tuple):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _object_list(value: Any, *, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise SchemaMismatch(f"agent.report {field} must be an array of objects.")
    return [dict(item) for item in value]


def _bounded_positive_int(value: Any, *, default: int, upper: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, upper))


def _parent_remaining_timeout_seconds(parent: Goal) -> int:
    if parent.timeout <= 0:
        return MAX_CHILD_TIMEOUT_SECONDS
    remaining = parent.created_at + parent.timeout - time.time()
    return max(1, int(remaining))


def _intersect_budget(
    value: Any,
    *,
    default: int,
    system_limit: int,
    parent_limit: int,
) -> int:
    requested = _bounded_positive_int(value, default=default, upper=system_limit)
    return min(requested, parent_limit) if parent_limit > 0 else requested


def _intersect_float_budget(
    value: Any,
    *,
    default: float,
    system_limit: float,
    parent_limit: float,
) -> float:
    try:
        requested = float(value)
    except (TypeError, ValueError):
        requested = default
    requested = max(0.01, min(requested, system_limit))
    return min(requested, parent_limit) if parent_limit > 0 else requested
