from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .capabilities_types import (
    Capability,
    CapabilityContext,
    CapabilityNode,
    CapabilityProvider,
    CapabilityResult,
)
from .capability_contract import (
    CAPABILITY_ACTION_APPROVAL,
    CAPABILITY_ERROR_REASON_KEY,
    CAPABILITY_REASON_KEY,
    CAPABILITY_REASON_SENSITIVE_APPROVAL,
)
from .approval_contract import (
    APPROVAL_ACTION_CAPABILITY,
    APPROVAL_STATUS_PENDING,
)
from .hooks import HookDecision, HookEvent, HookRegistry
from .effect_journal import EffectJournal
from .json_utils import json_schema_errors
from .lifecycle import Governance, Phase, Resolution
from .operating_context import permission_allows
from .resource_gateway import GlobalResourceGateway, ResourceLimits, ResourceRequest
from .runs import RunStore
from .safeguards import (
    CapabilityRiskAssessment,
    assess_capability_call,
    call_mutates,
    prepare_capability_call,
    required_permission_for_call,
    workspace_boundary_facts,
)
from .tools import API_CONTEXT, TURN_CONTEXT, ToolSpec, build_tool_gateway
from .actions.registry import ActionCapabilityProvider  # noqa: F401
from .actions.tools import ToolGatewayCapabilityProvider, ToolCapability

logger = logging.getLogger("navi.capabilities")


def _capability_error(
    *,
    action: str,
    error_reason: str,
    message: str,
    observation_facts: dict[str, Any],
    facts: dict[str, Any] | None = None,
    terminal: bool = True,
) -> CapabilityResult:
    fact_payload = {CAPABILITY_ERROR_REASON_KEY: error_reason, **observation_facts, **(facts or {})}
    return CapabilityResult(
        ok=False,
        action=action,
        message=message,
        terminal=terminal,
        facts=fact_payload,
        error_reason=error_reason,
    )


class CapabilityRegistry:
    """Agent OS syscall table.

    The model sees declared capabilities and chooses one. The kernel does not
    know capability names; it only asks this registry to validate and invoke.
    """

    def __init__(
        self,
        *,
        home: Path,
        project_dir: Path,
        allow_sources: set[str] | None = None,
        allowed_tools: set[str] | None = None,
        disabled_tools: set[str] | None = None,
        disabled_capability_classes: frozenset[str] | frozenset = frozenset(),
        permission_ceiling: str = "write",
        execution_context: str = TURN_CONTEXT,
        governed_run_id: str | None = None,
        sensitive_approval_mode: str = "enforce",
        runtime: Any | None = None,
        resource_gateway: GlobalResourceGateway | None = None,
    ) -> None:
        self.home = home
        self.allow_sources = allow_sources
        self.allowed_tools = allowed_tools
        self.disabled_tools = disabled_tools or set()
        self.disabled_capability_classes = disabled_capability_classes
        self.permission_ceiling = permission_ceiling
        self.execution_context = execution_context
        self.sensitive_approval_mode = sensitive_approval_mode
        self.runtime = runtime
        # When set, this registry executes on behalf of an approved background
        # run. Sensitive (mutating) ops are then gated by a per-capability
        # approval: the first such op suspends the run for a fresh code instead
        # of running unchecked. Replay after approval passes the recorded grant.
        self.governed_run_id = governed_run_id or ""
        self.gateway = build_tool_gateway(
            home,
            project_dir=project_dir,
        )
        self.resource_gateway = resource_gateway or GlobalResourceGateway(ResourceLimits())
        self.providers: tuple[CapabilityProvider, ...] = (
            ActionCapabilityProvider(
                home=self.home,
                gateway=self.gateway,
                runtime=self.runtime,
                capability_registry=self,
            ),
            ToolGatewayCapabilityProvider(self.gateway, capability_registry=self),
        )
        self.hooks = HookRegistry(home)
        self.handlers = self._build_handlers()

    def refresh(self) -> None:
        self.gateway.refresh()
        self.handlers = self._build_handlers()

    def planner_specs(
        self,
        *,
        permission_ceiling: str | None = None,
    ) -> list[ToolSpec]:
        # Capability discovery is a fact surface, not an authorization
        # decision.  The caller's policy envelope and concrete-call approval
        # gate are enforced by ``invoke``.
        del permission_ceiling
        return sorted(
            [handler.spec for handler in self.handlers.values()],
            key=lambda spec: spec.name,
        )

    def list_specs(self) -> list[ToolSpec]:
        return self.planner_specs()

    def capability_graph(self, *, permission_ceiling: str | None = None) -> list[CapabilityNode]:
        del permission_ceiling
        nodes = []
        for handler in self.handlers.values():
            spec = handler.spec
            nodes.append(
                CapabilityNode(
                    name=spec.name,
                    source=spec.source,
                    permission=spec.permission,
                    facts_only=spec.facts_only,
                    mutates=spec.mutates,
                    input_schema=spec.input_schema,
                    output_schema=spec.output_schema,
                    provider="tool_gateway" if isinstance(handler, ToolCapability) else "action",
                    description=spec.description,
                    side_effect_policy=spec.side_effect_policy.to_dict(),
                    permission_policy=spec.permission_policy,
                    argument_permission_field=spec.argument_permission_field,
                    argument_permissions=spec.argument_permissions,
                    risk_policy=spec.risk_policy,
                    context_policy=spec.context_policy,
                    runtime_policy=spec.runtime_policy,
                    delegation_allowed=spec.delegation_allowed,
                    deterministic_completion_authority=(spec.deterministic_completion_authority),
                    approval_policy=spec.approval_policy,
                    workspace_policy=spec.workspace_policy,
                    workspace_fields=spec.workspace_fields,
                    workspace_scope=spec.workspace_scope,
                )
            )
        return sorted(nodes, key=lambda node: node.name)

    def list_sources(self) -> list[str]:
        return sorted({handler.spec.source for handler in self.handlers.values()})

    def get(self, name: str) -> ToolSpec | None:
        handler = self.handlers.get(name)
        return handler.spec if handler else None

    async def invoke(
        self,
        name: str,
        args: dict[str, Any] | None,
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        context = replace(context, execution_context=self.execution_context)
        handler = self.handlers.get(name)
        if handler is None:
            return _capability_error(
                action=f"execute:{name}",
                error_reason="not_found",
                message=f"capability not found: {name}",
                observation_facts={"tool": name},
            )
        if not handler.spec.available_in(self.execution_context):
            return _capability_error(
                action=f"execute:{name}",
                error_reason="execution_context_unavailable",
                message=f"capability {name} is not available in execution context {self.execution_context}",
                observation_facts={
                    "tool": name,
                    "execution_context": self.execution_context,
                },
            )
        if self.allowed_tools is not None and name not in self.allowed_tools:
            return _capability_error(
                action=f"execute:{name}",
                error_reason="policy_envelope",
                message=f"capability {name} is outside the current policy envelope",
                observation_facts={
                    "tool": name,
                    "allowed_tools": sorted(self.allowed_tools),
                },
            )
        actual_ceiling = context.permission_ceiling
        if not permission_allows(handler.spec.permission, permission):
            return _capability_error(
                action=f"execute:{name}",
                error_reason="permission_escalation",
                message=f"capability {name} requires {handler.spec.permission} but requested {permission}",
                observation_facts={
                    "tool": name,
                    "requested": permission,
                    "required": handler.spec.permission,
                },
            )
        call_args = args or {}
        input_schema_errors = json_schema_errors(call_args, handler.spec.input_schema)
        if input_schema_errors:
            return _capability_error(
                action=f"execute:{name}",
                error_reason="schema_mismatch",
                message=f"capability {name} input schema mismatch",
                observation_facts={
                    "tool": name,
                    "schema_errors": input_schema_errors,
                },
            )
        workspace_facts = workspace_boundary_facts(
            handler.spec,
            call_args,
            workspace=(
                context.workspace
                if handler.spec.workspace_scope == "context"
                else str(self.gateway.project_dir)
            ),
        )
        if not workspace_facts["allowed"]:
            return _capability_error(
                action=f"execute:{name}",
                error_reason="resource_scope_violation",
                message=f"capability {name} requested a path outside its workspace",
                observation_facts={"tool": name, "workspace_boundary": workspace_facts},
            )
        call_args, preparation_error = prepare_capability_call(handler.spec, call_args)
        if preparation_error is not None:
            return _capability_error(
                action=f"execute:{name}",
                error_reason=str(preparation_error["error_reason"]),
                message=f"capability {name} could not prepare its external target",
                observation_facts={"tool": name, "target": preparation_error},
                terminal=False,
            )
        try:
            preflight = getattr(handler, "preflight", None)
            preflight_result = (
                await preflight(call_args, permission=permission, context=context)
                if callable(preflight)
                else None
            )
        except Exception as exc:
            logger.exception("Capability preflight failed for %s", name)
            return _capability_error(
                action=f"execute:{name}",
                error_reason="preflight_failed",
                message=f"capability {name} preflight failed: {exc}",
                observation_facts={"tool": name, "error_type": type(exc).__name__},
            )
        if preflight_result is not None and not preflight_result.ok:
            return preflight_result
        effective_required_permission = required_permission_for_call(
            handler.spec,
            call_args,
        )
        approval_permission = (
            permission
            if permission_allows(effective_required_permission, permission)
            else effective_required_permission
        )
        ceiling_exceeded = not permission_allows(
            approval_permission,
            actual_ceiling,
        )
        ceiling_risk = (
            CapabilityRiskAssessment(
                risk_class="high",
                sensitive_contexts=("permission_ceiling",),
                confirmation_required=True,
                reason_code="permission_ceiling_requires_approval",
                evidence={
                    "tool": name,
                    "requested_permission": permission,
                    "effective_required_permission": effective_required_permission,
                    "permission_ceiling": actual_ceiling,
                },
            )
            if ceiling_exceeded
            else None
        )
        approval_risk, approved_approval_id = self._approval_state_for_call(
            handler.spec,
            name,
            approval_permission,
            call_args,
            context=context,
            required_risk=ceiling_risk,
        )
        if ceiling_exceeded and approval_risk is None and not approved_approval_id:
            return _capability_error(
                action=f"execute:{name}",
                error_reason="permission_ceiling",
                message=(
                    f"capability {name} call requires {approval_permission} "
                    f"but the permission ceiling is {actual_ceiling}"
                ),
                observation_facts={
                    "tool": name,
                    "requested": permission,
                    "required": effective_required_permission,
                    "permission_ceiling": actual_ceiling,
                    "call_dependent_permission": True,
                },
            )
        permission_underdeclared = not permission_allows(
            effective_required_permission,
            permission,
        )
        if permission_underdeclared and approval_risk is None and not approved_approval_id:
            return _capability_error(
                action=f"execute:{name}",
                error_reason="permission_escalation",
                message=(
                    f"capability {name} call requires {effective_required_permission} "
                    f"but requested {permission}"
                ),
                observation_facts={
                    "tool": name,
                    "requested": permission,
                    "required": effective_required_permission,
                    "call_dependent_permission": True,
                },
            )
        if approval_risk is not None:
            if not self.governed_run_id:
                return self._suspend_turn_for_sensitive_approval(
                    handler.spec,
                    name,
                    approval_permission,
                    call_args,
                    risk=approval_risk,
                    context=context,
                )
            return self._suspend_for_sensitive_approval(
                handler.spec,
                name,
                approval_permission,
                call_args,
                risk=approval_risk,
                context=context,
            )
        if approved_approval_id:
            context = replace(
                context,
                approved_approval_id=approved_approval_id,
            )
        execution_permission = effective_required_permission
        call_has_effect = call_mutates(handler.spec, call_args)
        before_decisions = self.hooks.run(
            HookEvent(
                event="before_capability",
                payload={
                    "tool": name,
                    "permission": execution_permission,
                    "source": context.source,
                    "sender_id": context.sender_id,
                    "workspace": context.workspace,
                    "mutates": call_has_effect,
                    "side_effect_policy": handler.spec.side_effect_policy.to_dict(),
                    "args_keys": sorted(call_args),
                },
            )
        )
        blocked = _blocking_hook(before_decisions)
        if blocked is not None:
            facts = {"hook_decision": asdict(blocked)}
            return _capability_error(
                action=f"execute:{name}",
                error_reason="hook_blocked",
                message=blocked.reason_code or f"hook_blocked:{blocked.hook}",
                observation_facts={"tool": name, "hook": blocked.hook},
                facts=facts,
            )
        effect_journal: EffectJournal | None = None
        effect_owner = ""
        effect_key = context.effect_idempotency_key
        if call_has_effect and context.loop_run_id and effect_key:
            effect_journal = EffectJournal(self.home)
            effect_owner = f"capability:{uuid.uuid4().hex}"
            reservation = effect_journal.reserve(
                effect_key=effect_key,
                loop_run_id=context.loop_run_id,
                tool=name,
                owner=effect_owner,
            )
            if reservation.status == "replay" and reservation.result is not None:
                return _capability_result_from_dict(reservation.result)
            if reservation.status in {"busy", "uncertain"}:
                return _capability_error(
                    action=f"execute:{name}",
                    error_reason=f"effect_{reservation.status}",
                    message=f"mutating capability effect is {reservation.status}",
                    observation_facts={
                        "tool": name,
                        "effect_key": effect_key,
                        "effect_status": reservation.status,
                    },
                    terminal=False,
                )
        resource_grant = self.resource_gateway.request(
            ResourceRequest(kind=f"capability:{name}", units=1)
        )
        if not resource_grant.allowed:
            if effect_journal is not None:
                effect_journal.abandon(effect_key, owner=effect_owner)
            return _capability_error(
                action=f"execute:{name}",
                error_reason=f"resource_{resource_grant.decision}",
                message=f"resource gateway {resource_grant.decision}: {resource_grant.reason}",
                observation_facts={
                    "tool": name,
                    "resource_grant": resource_grant.to_dict(),
                },
                terminal=False,
            )
        started_at = time.time()
        audit_log_id = ""
        if call_has_effect and not isinstance(handler, ToolCapability):
            try:
                audit_log_id = self._reserve_action_audit(
                    handler.spec,
                    call_args,
                    context=context,
                    started_at=started_at,
                )
            except Exception as exc:
                self.resource_gateway.release(grant_id=resource_grant.grant_id)
                if effect_journal is not None:
                    effect_journal.abandon(effect_key, owner=effect_owner)
                logger.error(
                    "mutating action audit reservation failed for %s: %s",
                    name,
                    exc,
                    exc_info=True,
                )
                return _capability_error(
                    action=f"execute:{name}",
                    error_reason="audit_unavailable",
                    message=(
                        f"capability {name} was not executed because audit persistence "
                        "is unavailable"
                    ),
                    observation_facts={
                        "tool": name,
                        "audit_phase": "reservation",
                        "error_type": type(exc).__name__,
                    },
                    terminal=False,
                )
        try:
            result = await handler.invoke(
                call_args,
                permission=execution_permission,
                context=context,
            )
        except Exception as exc:
            if effect_journal is not None:
                effect_journal.fail(
                    effect_key,
                    owner=effect_owner,
                    error=f"{type(exc).__name__}: {exc}",
                )
            logger.exception(f"Unhandled exception in capability {name}: {exc}")
            result = _capability_error(
                action=f"execute:{name}",
                error_reason="internal_error",
                message=f"capability {name} crashed: {exc}",
                observation_facts={"tool": name, "error_type": type(exc).__name__},
                terminal=False,
            )
        finally:
            self.resource_gateway.release(grant_id=resource_grant.grant_id)
        if result.ok:
            output_schema_errors = json_schema_errors(
                result.facts or {}, handler.spec.output_schema
            )
            if output_schema_errors:
                result = _capability_error(
                    action=f"execute:{name}",
                    error_reason="schema_mismatch",
                    message=f"capability {name} output schema mismatch",
                    observation_facts={
                        "tool": name,
                        "schema_errors": output_schema_errors,
                        "result_action": result.action,
                    },
                    terminal=False,
                )
        if audit_log_id:
            try:
                self._complete_action_audit(audit_log_id, result)
            except Exception as exc:
                if effect_journal is not None:
                    effect_journal.fail(
                        effect_key,
                        owner=effect_owner,
                        error=f"audit completion failed: {type(exc).__name__}: {exc}",
                    )
                logger.error(
                    "mutating action audit completion failed for %s: %s",
                    name,
                    exc,
                    exc_info=True,
                )
                return _capability_error(
                    action=f"execute:{name}",
                    error_reason="audit_completion_failed",
                    message=(
                        f"capability {name} completed its effect but the audit outcome "
                        "could not be persisted"
                    ),
                    observation_facts={
                        "tool": name,
                        "audit_phase": "completion",
                        "audit_reservation_id": audit_log_id,
                        "effect_result_ok": result.ok,
                        "error_type": type(exc).__name__,
                    },
                    terminal=False,
                )
        if effect_journal is not None:
            effect_journal.complete(
                effect_key,
                owner=effect_owner,
                result=asdict(result),
            )
        self.hooks.run(
            HookEvent(
                event="after_capability",
                payload={
                    "tool": name,
                    "permission": execution_permission,
                    "source": context.source,
                    "sender_id": context.sender_id,
                    "workspace": context.workspace,
                    "ok": result.ok,
                    "action": result.action,
                    "run_id": result.run_id,
                    "fact_keys": sorted((result.facts or {}).keys()),
                },
            )
        )
        if not call_has_effect and not isinstance(handler, ToolCapability):
            self._audit_action_capability(handler.spec, call_args, result, started_at=started_at)
        return result

    def _approval_state_for_call(
        self,
        spec: ToolSpec,
        name: str,
        permission: str,
        call_args: dict[str, Any],
        *,
        context: CapabilityContext,
        required_risk: CapabilityRiskAssessment | None = None,
    ) -> tuple[CapabilityRiskAssessment | None, str]:
        if self.sensitive_approval_mode == "skip":
            return None, ""
        if spec.approval_policy == "control_plane":
            return None, ""
        if spec.approval_policy == "explicit_control" and self.execution_context == API_CONTEXT:
            return None, ""
        risk = assess_capability_call(
            spec,
            call_args,
            workspace=(
                context.workspace
                if spec.workspace_scope == "context"
                else str(self.gateway.project_dir)
            ),
        )
        if required_risk is not None and (
            not risk.confirmation_required and risk.risk_class != "high"
        ):
            risk = required_risk
        if not risk.confirmation_required and risk.risk_class != "high":
            return None, ""
        args_json = _canonical_args_json(call_args, home=self.home)
        runs = RunStore(self.home)
        if self.governed_run_id:
            approved = runs.approved_approval_for_run(
                self.governed_run_id,
                action=APPROVAL_ACTION_CAPABILITY,
                requested_tool=name,
                requested_permission=permission,
                args_json=args_json,
            )
        else:
            approved = self._approved_turn_capability_approval(
                runs,
                name=name,
                permission=permission,
                args_json=args_json,
                context=context,
            )
        return (None, approved.id) if approved is not None else (risk, "")

    def _suspend_turn_for_sensitive_approval(
        self,
        spec: ToolSpec,
        name: str,
        permission: str,
        call_args: dict[str, Any],
        *,
        risk: CapabilityRiskAssessment,
        context: CapabilityContext,
    ) -> CapabilityResult:
        runs = RunStore(self.home)
        args_json = _canonical_args_json(call_args, home=self.home)
        run, approval, transition = self._active_turn_capability_approval(
            runs,
            name=name,
            permission=permission,
            args_json=args_json,
            context=context,
        )
        if run is None or approval is None:
            run = runs.create(
                f"Approve sensitive capability {name}",
                kind="capability_approval",
                prompt=f"Capability approval requested for {name}",
                source=context.source,
                peer_id=context.peer_id,
                sender_id=context.sender_id,
                workspace=context.workspace,
                phase=Phase.PAUSED,
                governance=Governance.AWAITING_APPROVAL,
                resolution=Resolution.BLOCKED,
                why_now="trigger=active_turn_sensitive_capability",
            )
            approval = runs.create_approval(
                run_id=run.id,
                action=APPROVAL_ACTION_CAPABILITY,
                source=context.source,
                peer_id=context.peer_id,
                sender_id=context.sender_id,
                requested_tool=name,
                requested_permission=permission,
                args_json=args_json,
                reason=f"{risk.reason_code}: {name} ({risk.risk_class})",
            )
            run = (
                runs.update_run(
                    run.id,
                    plan_summary=f"capability_approval:{name}:{permission}:{args_json}",
                    result_summary="",
                    error="",
                )
                or run
            )
            transition = "created"
        facts = {
            CAPABILITY_REASON_KEY: CAPABILITY_REASON_SENSITIVE_APPROVAL,
            "entity_type": "approval_request",
            "entity_id": approval.id,
            "state_transition": transition,
            "turn_scope": "current",
            "run_id": run.id,
            "status": APPROVAL_STATUS_PENDING,
            "requested_tool": name,
            "requested_permission": permission,
            "risk": risk.to_facts(),
            "approval": {
                "id": approval.id,
                "run_id": approval.run_id,
                "action": approval.action,
                "requested_tool": approval.requested_tool,
                "requested_permission": approval.requested_permission,
                "code": approval.code,
                "expires_at": approval.expires_at,
            },
        }
        return CapabilityResult(
            ok=False,
            action=CAPABILITY_ACTION_APPROVAL,
            message="",
            run_id=run.id,
            terminal=False,
            facts=facts,
            error_reason=CAPABILITY_REASON_SENSITIVE_APPROVAL,
            yields_control=True,
        )

    def _active_turn_capability_approval(
        self,
        runs: RunStore,
        *,
        name: str,
        permission: str,
        args_json: str,
        context: CapabilityContext,
    ):
        marker = f"capability_approval:{name}:{permission}:{args_json}"
        for run in runs.list_by_phases_scoped(
            [Phase.PAUSED, Phase.PENDING, Phase.RUNNING],
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            workspace=context.workspace,
            kind="capability_approval",
            plan_summary=marker,
            limit=100,
        ):
            approval = runs.pending_approval_for_run(
                run.id,
                source=context.source,
                peer_id=context.peer_id,
                sender_id=context.sender_id,
                action=APPROVAL_ACTION_CAPABILITY,
                requested_tool=name,
                requested_permission=permission,
                args_json=args_json,
            )
            if approval is not None:
                return run, approval, "existing"
        return None, None, ""

    def _approved_turn_capability_approval(
        self,
        runs: RunStore,
        *,
        name: str,
        permission: str,
        args_json: str,
        context: CapabilityContext,
    ):
        marker = f"capability_approval:{name}:{permission}:{args_json}"
        for run in runs.list_by_phases_scoped(
            [Phase.PENDING, Phase.RUNNING],
            source=context.source,
            peer_id=context.peer_id,
            sender_id=context.sender_id,
            workspace=context.workspace,
            kind="capability_approval",
            plan_summary=marker,
            limit=100,
        ):
            approval = runs.approved_approval_for_run(
                run.id,
                action=APPROVAL_ACTION_CAPABILITY,
                requested_tool=name,
                requested_permission=permission,
                args_json=args_json,
            )
            if approval is not None:
                return approval
        return None

    def _suspend_for_sensitive_approval(
        self,
        spec: ToolSpec,
        name: str,
        permission: str,
        call_args: dict[str, Any],
        *,
        risk: CapabilityRiskAssessment,
        context: CapabilityContext,
    ) -> CapabilityResult:
        runs = RunStore(self.home)
        run = runs.get(self.governed_run_id or "")
        source = context.source or (run.source if run else "")
        peer_id = context.peer_id or (run.peer_id if run else "")
        sender_id = context.sender_id or (run.sender_id if run else "")
        args_json = _canonical_args_json(call_args, home=self.home)
        approval = runs.pending_approval_for_run(
            self.governed_run_id or "",
            source=source,
            peer_id=peer_id,
            sender_id=sender_id,
            action=APPROVAL_ACTION_CAPABILITY,
            requested_tool=name,
            requested_permission=permission,
            args_json=args_json,
        )
        if approval is None:
            approval = runs.create_approval(
                run_id=self.governed_run_id or "",
                action=APPROVAL_ACTION_CAPABILITY,
                source=source,
                peer_id=peer_id,
                sender_id=sender_id,
                requested_tool=name,
                requested_permission=permission,
                args_json=args_json,
                reason=f"{risk.reason_code}: {name} ({risk.risk_class})",
            )
        runs.update_run(
            self.governed_run_id or "",
            phase=Phase.PAUSED,
            governance=Governance.AWAITING_APPROVAL,
            resolution=Resolution.BLOCKED,
            result_summary="",
            error="",
        )
        facts = {
            CAPABILITY_REASON_KEY: CAPABILITY_REASON_SENSITIVE_APPROVAL,
            "entity_type": "approval_request",
            "entity_id": approval.id,
            "state_transition": "created",
            "turn_scope": "current",
            "run_id": self.governed_run_id or "",
            "status": APPROVAL_STATUS_PENDING,
            "requested_tool": name,
            "requested_permission": permission,
            "risk": risk.to_facts(),
            "approval": {
                "id": approval.id,
                "run_id": approval.run_id,
                "action": approval.action,
                "requested_tool": approval.requested_tool,
                "requested_permission": approval.requested_permission,
                "code": approval.code,
                "expires_at": approval.expires_at,
            },
        }
        return CapabilityResult(
            ok=False,
            action=CAPABILITY_ACTION_APPROVAL,
            message="",
            run_id=self.governed_run_id or "",
            terminal=True,
            facts=facts,
            error_reason=CAPABILITY_REASON_SENSITIVE_APPROVAL,
            yields_control=True,
        )

    def _build_handlers(self) -> Mapping[str, Capability]:
        handlers: dict[str, Capability] = {}
        for provider in self.providers:
            handlers.update(provider.capabilities())

        def _is_class_blocked(name: str) -> bool:
            spec = handlers[name].spec
            return spec.capability_class in self.disabled_capability_classes

        filtered = {
            name: handler
            for name, handler in handlers.items()
            if name not in self.disabled_tools
            and not _is_class_blocked(name)
            and (self.allow_sources is None or handler.spec.source in self.allow_sources)
            and handler.spec.available_in(self.execution_context)
        }
        return filtered

    def _reserve_action_audit(
        self,
        spec: ToolSpec,
        args: dict[str, Any],
        *,
        context: CapabilityContext,
        started_at: float,
    ) -> str:
        from .safeguards import redact_personal_data_deep

        log = RunStore(self.home).add_tool_call_log(
            tool=spec.name,
            args_json=json.dumps(
                redact_personal_data_deep(args), ensure_ascii=False, sort_keys=True
            ),
            ok=False,
            facts_json=json.dumps(
                {"audit_phase": "reserved", "mutates": True},
                ensure_ascii=False,
                sort_keys=True,
            ),
            error="execution outcome pending",
            started_at=started_at,
            ended_at=started_at,
            run_id=self.governed_run_id or context.loop_run_id,
            trace_id=context.trace_id,
        )
        return log.id

    def _complete_action_audit(
        self,
        log_id: str,
        result: CapabilityResult,
    ) -> None:
        from .safeguards import redact_personal_data, redact_personal_data_deep

        facts = result.facts or {
            "action": result.action,
            "run_id": result.run_id,
            "terminal": result.terminal,
        }
        RunStore(self.home).complete_tool_call_log(
            log_id,
            ok=result.ok,
            facts_json=json.dumps(
                {"audit_phase": "completed", **redact_personal_data_deep(facts)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            error=(
                "" if result.ok else redact_personal_data(result.message or result.error_reason)
            ),
            ended_at=time.time(),
        )

    def _audit_action_capability(
        self,
        spec: ToolSpec,
        args: dict[str, Any],
        result: CapabilityResult,
        *,
        started_at: float,
    ) -> None:
        facts = result.facts or {
            "action": result.action,
            "run_id": result.run_id,
            "terminal": result.terminal,
        }
        try:
            from .safeguards import redact_personal_data, redact_personal_data_deep

            RunStore(self.home).add_tool_call_log(
                tool=spec.name,
                args_json=json.dumps(
                    redact_personal_data_deep(args), ensure_ascii=False, sort_keys=True
                ),
                ok=result.ok,
                facts_json=json.dumps(
                    redact_personal_data_deep(facts), ensure_ascii=False, sort_keys=True
                ),
                error=(
                    "" if result.ok else redact_personal_data(result.message or result.error_reason)
                ),
                started_at=started_at,
                ended_at=time.time(),
                run_id=self.governed_run_id or "",
            )
        except Exception as exc:
            logger.error(
                "action capability audit log failed for %s: %s", spec.name, exc, exc_info=True
            )


def _blocking_hook(decisions: list[HookDecision]) -> HookDecision | None:
    return next((decision for decision in decisions if decision.decision == "block"), None)


def _capability_result_from_dict(value: dict[str, Any]) -> CapabilityResult:
    return CapabilityResult(
        ok=bool(value.get("ok", False)),
        action=str(value.get("action") or "error"),
        message=str(value.get("message") or ""),
        run_id=str(value.get("run_id") or ""),
        terminal=bool(value.get("terminal", False)),
        facts=dict(value.get("facts") or {}),
        provenance=str(value.get("provenance") or ""),
        error_reason=str(value.get("error_reason") or ""),
        yields_control=bool(value.get("yields_control", False)),
    )


def _canonical_args_json(value: dict[str, Any], *, home: Path) -> str:
    from .safeguards import canonical_approval_args_json

    return canonical_approval_args_json(value, home=home)


def build_capability_registry(
    home: Path,
    *,
    project_dir: Path,
    allow_sources: set[str] | None = None,
    allowed_tools: set[str] | None = None,
    disabled_tools: set[str] | None = None,
    permission_ceiling: str = "write",
    execution_context: str = TURN_CONTEXT,
    governed_run_id: str | None = None,
    runtime: Any | None = None,
    resource_gateway: GlobalResourceGateway | None = None,
) -> CapabilityRegistry:
    return CapabilityRegistry(
        home=home,
        project_dir=project_dir,
        allow_sources=allow_sources,
        allowed_tools=allowed_tools,
        disabled_tools=disabled_tools,
        permission_ceiling=permission_ceiling,
        execution_context=execution_context,
        governed_run_id=governed_run_id,
        runtime=runtime,
        resource_gateway=resource_gateway,
    )
