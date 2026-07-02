from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
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
from .json_utils import json_schema_errors
from .lifecycle import RUN_STATUS_AWAITING_APPROVAL, RUN_STATUS_PENDING
from .operating_context import PERMISSION_ORDER, permission_allows
from .runs import RunStore
from .tools import TURN_CONTEXT, ToolSpec, build_tool_gateway
from .actions.registry import ActionCapabilityProvider  # noqa: F401
from .actions.tools import ToolGatewayCapabilityProvider, ToolCapability, ToolsListCapability

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
    observation_payload = {
        CAPABILITY_ERROR_REASON_KEY: error_reason,
        **observation_facts,
    }
    return CapabilityResult(
        ok=False,
        action=action,
        observation=json.dumps(observation_payload, ensure_ascii=False, sort_keys=True),
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
        enforce_connector_source_policy: bool = True,
        governed_run_id: str | None = None,
    ):
        self.home = home
        self.allow_sources = allow_sources
        self.allowed_tools = allowed_tools
        self.disabled_tools = disabled_tools or set()
        self.disabled_capability_classes = disabled_capability_classes
        self.permission_ceiling = permission_ceiling
        self.execution_context = execution_context
        # When True (live ingress), invoke() re-derives the remote connector
        # sandbox from context.source as defense-in-depth. The approved,
        # already-governed background executor sets this False so it is not
        # re-sandboxed by the surface its task happened to originate from.
        self.enforce_connector_source_policy = enforce_connector_source_policy
        # When set, this registry executes on behalf of an approved background
        # run. Sensitive (mutating) ops are then gated by a per-capability
        # approval: the first such op suspends the run for a fresh code instead
        # of running unchecked. Replay after approval passes the recorded grant.
        self.governed_run_id = governed_run_id
        self.gateway = build_tool_gateway(
            home,
            project_dir=project_dir,
            allow_sources=allow_sources,
            allowed_tools=allowed_tools,
            disabled_tools=disabled_tools,
            permission_ceiling="write",
        )
        self.providers: tuple[CapabilityProvider, ...] = (
            ActionCapabilityProvider(home=self.home, gateway=self.gateway),
            ToolGatewayCapabilityProvider(self.gateway),
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
        context: CapabilityContext | None = None,
    ) -> list[ToolSpec]:
        ceiling = permission_ceiling or self.permission_ceiling
        return sorted(
            [
                handler.spec
                for handler in self.handlers.values()
                if permission_allows(handler.spec.permission, ceiling)
                and self._source_policy_allows_spec(handler.spec, context)
            ],
            key=lambda spec: spec.name,
        )

    def list_specs(self) -> list[ToolSpec]:
        return self.planner_specs()

    def capability_graph(self, *, permission_ceiling: str | None = None) -> list[CapabilityNode]:
        ceiling = permission_ceiling or self.permission_ceiling
        nodes = []
        for handler in self.handlers.values():
            spec = handler.spec
            if not permission_allows(spec.permission, ceiling):
                continue
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
                )
            )
        return sorted(nodes, key=lambda node: node.name)

    def list_sources(self) -> list[str]:
        return sorted({handler.spec.source for handler in self.handlers.values()})

    def _source_policy_allows_spec(
        self, spec: ToolSpec, context: CapabilityContext | None
    ) -> bool:
        if context is None or not self.enforce_connector_source_policy:
            return True
        source_policy = _connector_policy_for_source(context.source)
        if source_policy is None:
            return True
        limits = _source_policy_limits(self.home, context, source_policy)
        if spec.name in source_policy.blocked_tools:
            return False
        if spec.capability_class in source_policy.blocked_capability_classes:
            return False
        if limits.allowed_tools and spec.name not in limits.allowed_tools:
            return False
        return permission_allows(spec.permission, limits.permission_ceiling)

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
        actual_ceiling = context.permission_ceiling
        source_policy = (
            _connector_policy_for_source(context.source)
            if self.enforce_connector_source_policy
            else None
        )
        if source_policy is not None:
            limits = _source_policy_limits(self.home, context, source_policy)
            if name in source_policy.blocked_tools:
                return _capability_error(
                    action=f"execute:{name}",
                    error_reason="remote_tool_blocked",
                    message=f"remote connector policy blocks capability {name}",
                    observation_facts={
                        "tool": name,
                        "source": context.source,
                        "policy": source_policy.name,
                    },
                )
            if handler.spec.capability_class in source_policy.blocked_capability_classes:
                capability_class = handler.spec.capability_class
                return _capability_error(
                    action=f"execute:{name}",
                    error_reason="remote_capability_class_blocked",
                    message=f"remote connector policy blocks capability class {capability_class}",
                    observation_facts={
                        "tool": name,
                        "capability_class": capability_class,
                        "source": context.source,
                        "policy": source_policy.name,
                    },
                )
            if limits.allowed_tools and name not in limits.allowed_tools:
                return _capability_error(
                    action=f"execute:{name}",
                    error_reason="remote_tool_not_allowed",
                    message=f"remote connector policy blocks capability {name}",
                    observation_facts={
                        "tool": name,
                        "source": context.source,
                        "policy": source_policy.name,
                    },
                )
            if not permission_allows(permission, limits.permission_ceiling):
                ceiling = limits.permission_ceiling
                return _capability_error(
                    action=f"execute:{name}",
                    error_reason="remote_permission_ceiling",
                    message=f"remote connector ceiling {ceiling} blocks requested permission {permission}",
                    observation_facts={
                        "requested_permission": permission,
                        "permission_ceiling": ceiling,
                        "source": context.source,
                        "policy": source_policy.name,
                    },
                )
        if not permission_allows(permission, actual_ceiling):
            return _capability_error(
                action=f"execute:{name}",
                error_reason="permission_ceiling",
                message=f"permission ceiling {actual_ceiling} blocks requested permission {permission}",
                observation_facts={
                    "requested_permission": permission,
                    "permission_ceiling": actual_ceiling,
                },
            )
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
        if self._sensitive_call_needs_approval(handler.spec, name, permission, call_args):
            if not self.governed_run_id:
                return CapabilityResult(
                    ok=False,
                    action=f"execute:{name}",
                    observation=json.dumps({"error": "missing_governed_run"}),
                    terminal=False,
                    error_reason="missing_governed_run",
                    message="Sensitive capability requires a durable governed run context to mount an approval. Ephemeral conversational turns cannot mount approvals."
                )
            return self._suspend_for_sensitive_approval(
                handler.spec,
                name,
                permission,
                call_args,
                context=context,
            )
        before_decisions = self.hooks.run(
            HookEvent(
                event="before_capability",
                payload={
                    "tool": name,
                    "permission": permission,
                    "source": context.source,
                    "sender_id": context.sender_id,
                    "workspace": context.workspace,
                    "mutates": handler.spec.mutates,
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
        started_at = time.time()
        try:
            result = await handler.invoke(call_args, permission=permission, context=context)
        except Exception as exc:
            logger.exception(f"Unhandled exception in capability {name}: {exc}")
            return _capability_error(
                action=f"execute:{name}",
                error_reason="internal_error",
                message=f"capability {name} crashed: {exc}",
                observation_facts={"tool": name, "error_type": type(exc).__name__},
                terminal=False,
            )
        if result.ok:
            output_schema_errors = json_schema_errors(result.facts or {}, handler.spec.output_schema)
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
        self.hooks.run(
            HookEvent(
                event="after_capability",
                payload={
                    "tool": name,
                    "permission": permission,
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
        if handler.spec.mutates and not isinstance(handler, ToolCapability):
            self._audit_action_capability(handler.spec, call_args, result, started_at=started_at)
        return result

    def _sensitive_call_needs_approval(
        self,
        spec: ToolSpec,
        name: str,
        permission: str,
        call_args: dict[str, Any],
    ) -> bool:
        if spec.governance_exempt:
            return False
        if not spec.mutates and spec.permission != "write":
            return False
        args_json = _canonical_args_json(call_args)
        approved = RunStore(self.home).approved_approval_for_run(
            self.governed_run_id,
            action=APPROVAL_ACTION_CAPABILITY,
            requested_tool=name,
            requested_permission=permission,
            args_json=args_json,
        )
        return approved is None

    def _suspend_for_sensitive_approval(
        self,
        spec: ToolSpec,
        name: str,
        permission: str,
        call_args: dict[str, Any],
        *,
        context: CapabilityContext,
    ) -> CapabilityResult:
        runs = RunStore(self.home)
        run = runs.get(self.governed_run_id or "")
        source = context.source or (run.source if run else "")
        peer_id = context.peer_id or (run.peer_id if run else "")
        sender_id = context.sender_id or (run.sender_id if run else "")
        args_json = _canonical_args_json(call_args)
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
                reason=f"sensitive capability requires approval: {name}",
            )
        runs.update_run(
            self.governed_run_id or "",
            status=RUN_STATUS_AWAITING_APPROVAL,
            result_summary=(
                "approval_requested\n"
                f"run_id={self.governed_run_id or ''}\n"
                f"approval_id={approval.id}\n"
                f"action={approval.action}\n"
                f"approval_code={approval.code}\n"
                f"requested_tool={name}\n"
                f"requested_permission={permission}\n"
                f"status={APPROVAL_STATUS_PENDING}"
            ),
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
        visible = (
            "approval_requested\n"
            f"run_id={self.governed_run_id or ''}\n"
            f"approval_id={approval.id}\n"
            f"action={approval.action}\n"
            f"approval_code={approval.code}\n"
            f"requested_tool={name}\n"
            f"requested_permission={permission}\n"
            "reason=sensitive_capability_requires_approval"
        )
        return CapabilityResult(
            ok=True,
            action=CAPABILITY_ACTION_APPROVAL,
            observation=json.dumps(facts, ensure_ascii=False, sort_keys=True),
            message="",
            run_id=self.governed_run_id or "",
            terminal=False,
            facts=facts,
        )

    def _build_handlers(self) -> Mapping[str, Capability]:
        handlers: dict[str, Capability] = {}
        for provider in self.providers:
            handlers.update(provider.capabilities())

        def _is_class_blocked(name: str) -> bool:
            spec = handlers[name].spec
            if spec.capability_class in self.disabled_capability_classes:
                return True
            return False

        filtered = {
            name: handler
            for name, handler in handlers.items()
            if (self.allowed_tools is None or name in self.allowed_tools)
            and name not in self.disabled_tools
            and not _is_class_blocked(name)
            and (self.allow_sources is None or handler.spec.source in self.allow_sources)
            and handler.spec.available_in(self.execution_context)
        }
        tools_list = filtered.get("tools.list")
        if tools_list is not None:
            filtered["tools.list"] = ToolsListCapability(tools_list.spec, registry=self)
        return filtered

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
            from .safeguards import redact_secrets, redact_secrets_deep

            RunStore(self.home).add_tool_call_log(
                tool=spec.name,
                args_json=json.dumps(redact_secrets_deep(args), ensure_ascii=False, sort_keys=True),
                ok=result.ok,
                facts_json=json.dumps(
                    redact_secrets_deep(facts), ensure_ascii=False, sort_keys=True
                ),
                error="" if result.ok else redact_secrets(result.message or result.observation),
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


def _canonical_args_json(value: dict[str, Any]) -> str:
    from .safeguards import redact_secrets_deep

    return json.dumps(redact_secrets_deep(value or {}), ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class _SourcePolicyLimits:
    permission_ceiling: str
    allowed_tools: frozenset[str]


def _source_policy_limits(home: Path, context: CapabilityContext, source_policy) -> _SourcePolicyLimits:
    ceiling = source_policy.permission_ceiling
    allowed_tools = frozenset(source_policy.allowed_tools)
    approval = RunStore(home).active_session_elevation(
        source=context.source,
        peer_id=context.peer_id,
        sender_id=context.sender_id,
    )
    if approval is None:
        return _SourcePolicyLimits(permission_ceiling=ceiling, allowed_tools=allowed_tools)
    ceiling = _max_permission(ceiling, approval.requested_permission)
    if permission_allows("write", ceiling):
        allowed_tools = frozenset((*allowed_tools, *_remote_elevated_allowed_tools()))
    return _SourcePolicyLimits(permission_ceiling=ceiling, allowed_tools=allowed_tools)


def _max_permission(current: str, requested: str) -> str:
    current_level = PERMISSION_ORDER.get(current, 0)
    requested_level = PERMISSION_ORDER.get(requested, 0)
    return requested if requested_level > current_level else current


def _remote_elevated_allowed_tools() -> frozenset[str]:
    try:
        from .connector_runtime import REMOTE_ELEVATED_ALLOWED_TOOLS
    except ImportError:
        return frozenset()
    return REMOTE_ELEVATED_ALLOWED_TOOLS


def _connector_policy_for_source(source: str):
    raw = source.strip()
    if not raw:
        return None
    try:
        from .connector_registry import load_connector_adapters
        from .connector_runtime import REMOTE_CONNECTOR_TOOL_POLICY
    except ImportError as exc:
        logger.debug("connector adapters unavailable for source policy: %s", exc)
        return None
    connector_sources: set[str] = set()
    for adapter in load_connector_adapters():
        connector_sources.update({adapter.name, adapter.spec.surface, adapter.spec.local_source})
    if raw.startswith("connector.") or raw in connector_sources:
        return REMOTE_CONNECTOR_TOOL_POLICY
    return None




def build_capability_registry(
    home: Path,
    *,
    project_dir: Path,
    allow_sources: set[str] | None = None,
    allowed_tools: set[str] | None = None,
    disabled_tools: set[str] | None = None,
    permission_ceiling: str = "write",
    execution_context: str = TURN_CONTEXT,
    enforce_connector_source_policy: bool = True,
    governed_run_id: str | None = None,
) -> CapabilityRegistry:
    return CapabilityRegistry(
        home=home,
        project_dir=project_dir,
        allow_sources=allow_sources,
        allowed_tools=allowed_tools,
        disabled_tools=disabled_tools,
        permission_ceiling=permission_ceiling,
        execution_context=execution_context,
        enforce_connector_source_policy=enforce_connector_source_policy,
        governed_run_id=governed_run_id,
    )
