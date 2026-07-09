"""Core tool handlers."""
from __future__ import annotations
from ..safeguards import capability_safeguard_facts
from ..tools import ToolRegistry, ToolResult

def _tools_list(registry: ToolRegistry) -> ToolResult:
    specs = registry.list_specs()
    return ToolResult(
        tool="tools.list",
        ok=True,
        facts={
            "category": "tools",
            "definition": "callable gateway tools registered in this gateway context",
            "not_skills": True,
            "tools": [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "permission": spec.permission,
                    "facts_only": spec.facts_only,
                    "mutates": spec.mutates,
                    "source": spec.source,
                    "side_effect_policy": spec.side_effect_policy.to_dict(),
                    "input_properties": sorted((spec.input_schema.get("properties") or {}).keys()),
                    "required": list(spec.input_schema.get("required") or []),
                    "safeguards": capability_safeguard_facts(spec),
                }
                for spec in specs
            ],
            "count": len(specs),
        },
    )

