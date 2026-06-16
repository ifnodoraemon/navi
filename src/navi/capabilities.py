from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .capabilities_types import (
    Capability,
    CapabilityContext,
    CapabilityNode,
    CapabilityProvider,
    CapabilityResult,
)
from .hooks import HookDecision, HookEvent, HookRegistry
from .operating_context import permission_allows
from .runs import RunStore
from .safeguards import capability_safeguard_facts
from .tools import TURN_CONTEXT, ToolSpec, build_tool_gateway
from .actions.registry import ActionCapabilityProvider
from .actions.tools import ToolGatewayCapabilityProvider, ToolCapability, ToolsListCapability

logger = logging.getLogger("navi.capabilities")


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
    ):
        self.home = home
        self.allow_sources = allow_sources
        self.allowed_tools = allowed_tools
        self.disabled_tools = disabled_tools or set()
        self.disabled_capability_classes = disabled_capability_classes
        self.permission_ceiling = permission_ceiling
        self.execution_context = execution_context
        self.gateway = build_tool_gateway(
            home,
            project_dir=project_dir,
            allow_sources=allow_sources,
            allowed_tools=allowed_tools,
            disabled_tools=disabled_tools,
            permission_ceiling=permission_ceiling,
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

    def planner_specs(self, *, permission_ceiling: str | None = None) -> list[ToolSpec]:
        ceiling = permission_ceiling or self.permission_ceiling
        return sorted(
            [
                handler.spec
                for handler in self.handlers.values()
                if permission_allows(handler.spec.permission, ceiling)
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
            return CapabilityResult(
                ok=False,
                action="capability_error",
                observation=f"capability not found: {name}",
                message=f"capability not found: {name}",
                terminal=True,
            )
        if not handler.spec.available_in(self.execution_context):
            return CapabilityResult(
                ok=False,
                action="capability_error",
                observation=f"capability {name} is not available in execution context {self.execution_context}",
                message=f"capability {name} is not available in execution context {self.execution_context}",
                terminal=True,
            )
        actual_ceiling = context.permission_ceiling
        source_policy = _connector_policy_for_source(context.source)
        if source_policy is not None:
            if source_policy.allowed_tools and name not in source_policy.allowed_tools:
                return CapabilityResult(
                    ok=False,
                    action="capability_error",
                    observation=f"remote connector policy blocks capability {name}",
                    message=f"remote connector policy blocks capability {name}",
                    terminal=True,
                )
            if handler.spec.capability_class in source_policy.blocked_capability_classes:
                capability_class = handler.spec.capability_class
                return CapabilityResult(
                    ok=False,
                    action="capability_error",
                    observation=f"remote connector policy blocks capability class {capability_class}",
                    message=f"remote connector policy blocks capability class {capability_class}",
                    terminal=True,
                )
            if not permission_allows(permission, source_policy.permission_ceiling):
                ceiling = source_policy.permission_ceiling
                return CapabilityResult(
                    ok=False,
                    action="capability_error",
                    observation=f"remote connector ceiling {ceiling} blocks requested permission {permission}",
                    message=f"remote connector ceiling {ceiling} blocks requested permission {permission}",
                    terminal=True,
                )
        if not permission_allows(permission, actual_ceiling):
            return CapabilityResult(
                ok=False,
                action="capability_error",
                observation=f"permission ceiling {actual_ceiling} blocks requested permission {permission}",
                message=f"permission ceiling {actual_ceiling} blocks requested permission {permission}",
                terminal=True,
            )
        if not permission_allows(handler.spec.permission, permission):
            return CapabilityResult(
                ok=False,
                action="capability_error",
                observation=f"capability {name} is not available with permission {permission}",
                message=f"capability {name} is not available with permission {permission}",
                terminal=True,
            )
        call_args = args or {}
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
            return CapabilityResult(
                ok=False,
                action="capability_error",
                observation=blocked.reason or f"hook blocked capability: {blocked.hook}",
                message=blocked.reason or f"hook blocked capability: {blocked.hook}",
                terminal=True,
                facts=facts,
            )
        started_at = time.time()
        try:
            result = await handler.invoke(call_args, permission=permission, context=context)
        except Exception as exc:
            logger.exception(f"Unhandled exception in capability {name}: {exc}")
            result = CapabilityResult(
                ok=False,
                action="capability_error",
                observation=f"capability encountered an internal error: {exc}",
                message=f"capability {name} crashed: {exc}",
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
            and permission_allows(handler.spec.permission, self.permission_ceiling)
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
            RunStore(self.home).add_tool_call_log(
                tool=spec.name,
                args_json=json.dumps(args, ensure_ascii=False, sort_keys=True),
                ok=result.ok,
                facts_json=json.dumps(facts, ensure_ascii=False, sort_keys=True),
                error="" if result.ok else result.message or result.observation,
                started_at=started_at,
                ended_at=time.time(),
            )
        except Exception as exc:
            logger.error(
                "action capability audit log failed for %s: %s", spec.name, exc, exc_info=True
            )


def _blocking_hook(decisions: list[HookDecision]) -> HookDecision | None:
    return next((decision for decision in decisions if decision.decision == "block"), None)


def _connector_policy_for_source(source: str):
    raw = source.strip()
    if not raw:
        return None
    try:
        from .connector_registry import load_connector_adapters
        from .connector_runtime import REMOTE_CONNECTOR_TOOL_POLICY
    except Exception:
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
) -> CapabilityRegistry:
    return CapabilityRegistry(
        home=home,
        project_dir=project_dir,
        allow_sources=allow_sources,
        allowed_tools=allowed_tools,
        disabled_tools=disabled_tools,
        permission_ceiling=permission_ceiling,
        execution_context=execution_context,
    )
