"""Core tool handlers."""
from __future__ import annotations
import os
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse
from typing import Any
from ..config import load_config
from ..fact_tools import service_facts, run_facts
from ..hooks import HookRegistry
from ..memory import MemoryStore
from ..operating_context import permission_allows
from ..runs import Approval
from ..safeguards import capability_safeguard_facts
from ..skills import SkillStore
from ..tools import ALL_EXECUTION_CONTEXTS, ToolRegistry, ToolResult, ToolSpec

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
                    "input_properties": sorted((spec.input_schema.get("properties") or {}).keys()),
                    "required": list(spec.input_schema.get("required") or []),
                    "safeguards": capability_safeguard_facts(spec),
                }
                for spec in specs
            ],
            "count": len(specs),
        },
    )


