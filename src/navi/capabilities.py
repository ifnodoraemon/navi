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
        if not permission_allows(permission, context.permission_ceiling):
            return CapabilityResult(
                ok=False,
                action="capability_error",
                observation=f"permission ceiling {context.permission_ceiling} blocks requested permission {permission}",
                message=f"permission ceiling {context.permission_ceiling} blocks requested permission {permission}",
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


class ActionCapabilityProvider:
    def __init__(self, *, home: Path, gateway):
        self.home = home
        self.gateway = gateway

    def capabilities(self) -> Mapping[str, Capability]:
        from navi.actions.registry import get_action_handlers

        
        return get_action_handlers(self.home, self.gateway.project_dir)


class ToolGatewayCapabilityProvider:
    def __init__(self, gateway):
        self.gateway = gateway

    def capabilities(self) -> Mapping[str, Capability]:
        return {
            spec.name: ToolCapability(spec, gateway=self.gateway)
            for spec in self.gateway.list_specs()
        }


class ToolCapability:
    def __init__(self, spec: ToolSpec, *, gateway):
        self.spec = spec
        self.gateway = gateway

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        result = self.gateway.call(self.spec.name, args)
        observation = json.dumps(
            {
                "capability": self.spec.name,
                "ok": result.ok,
                "facts": result.facts,
                "error": result.error,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        return CapabilityResult(
            ok=result.ok,
            action="tool",
            observation=observation,
            message=result.error if not result.ok else "",
            facts=result.facts,
        )


class ToolsListCapability:
    def __init__(self, spec: ToolSpec, *, registry: CapabilityRegistry):
        self.spec = spec
        self.registry = registry

    async def invoke(
        self,
        args: dict[str, Any],
        *,
        permission: str,
        context: CapabilityContext,
    ) -> CapabilityResult:
        specs = self.registry.planner_specs(permission_ceiling=context.permission_ceiling)
        facts = {
            "category": "tools",
            "definition": "callable capabilities available in the current permission and source context",
            "not_skills": True,
            "tools": [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "permission": spec.permission,
                    "facts_only": spec.facts_only,
                    "mutates": spec.mutates,
                    "source": spec.source,
                    "input_properties": sorted((spec.input_schema.get("properties") or {}).keys()),
                    "required": list(spec.input_schema.get("required") or []),
                    "safeguards": capability_safeguard_facts(spec),
                }
                for spec in specs
            ],
            "count": len(specs),
        }
        return CapabilityResult(
            ok=True,
            action="tool",
            observation=json.dumps(
                {"capability": self.spec.name, "facts": facts},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            facts=facts,
        )


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
