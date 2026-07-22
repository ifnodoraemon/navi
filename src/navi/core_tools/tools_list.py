"""Core tool handlers."""
from __future__ import annotations
from ..tool_manifest import tool_manifest_facts
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
            "tools": [tool_manifest_facts(spec) for spec in specs],
            "count": len(specs),
        },
    )
