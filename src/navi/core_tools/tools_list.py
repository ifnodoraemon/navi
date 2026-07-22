"""Core tool handlers."""

from __future__ import annotations
from ..tool_manifest import tool_catalog_facts
from ..tools import ToolRegistry, ToolResult


def _tools_list(registry: ToolRegistry) -> ToolResult:
    return ToolResult(
        tool="tools.list",
        ok=True,
        facts=tool_catalog_facts(
            registry.list_specs(),
            definition="callable gateway tools registered in this gateway context",
        ),
    )
