from __future__ import annotations

import json
from typing import Any, Mapping

from navi.capabilities_types import Capability, CapabilityContext, CapabilityResult
from navi.safeguards import capability_safeguard_facts
from navi.tools import ToolSpec


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
    def __init__(self, spec: ToolSpec, *, registry):
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
