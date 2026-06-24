"""Core tool handlers."""
from __future__ import annotations
from pathlib import Path
from ..hooks import HookRegistry
from ..tools import ToolResult

def _hooks_list(home: Path) -> ToolResult:
    return ToolResult(tool="hooks.list", ok=True, facts=HookRegistry(home).list_facts())


