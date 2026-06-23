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

def _provider_config(home: Path) -> ToolResult:
    from .config import validate_config, load_config

    try:
        config = load_config(home)
        errors = validate_config(config, home)
        return ToolResult(
            tool="provider.config",
            ok=True,
            facts={
                "provider": config.model.provider,
                "kind": config.model.kind,
                "model": config.model.model,
                "api_base_url": config.model.api_base_url,
                "has_api_key": bool(config.model.api_key),
                "fallbacks": [
                    {
                        "provider": item.provider,
                        "kind": item.kind,
                        "model": item.model,
                        "api_base_url": item.api_base_url,
                        "has_api_key": bool(item.api_key),
                    }
                    for item in config.model.fallbacks
                ],
                "routes": {
                    role: {
                        "provider": item.provider,
                        "kind": item.kind,
                        "model": item.model,
                        "api_base_url": item.api_base_url,
                        "has_api_key": bool(item.api_key),
                        "fallback_count": len(item.fallbacks),
                    }
                    for role, item in config.model.routes.items()
                },
                "validation_errors": errors,
            },
        )
    except Exception as e:
        return ToolResult(
            tool="provider.config",
            ok=False,
            error=f"Failed to load config: {e}",
            facts={
                "provider": "",
                "kind": "",
                "model": "",
                "api_base_url": "",
                "has_api_key": False,
                "fallbacks": [],
                "routes": {},
                "validation_errors": [f"Failed to load config: {e}"],
            },
        )


