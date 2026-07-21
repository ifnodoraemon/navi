"""Core tool handlers."""
from __future__ import annotations
from pathlib import Path
from ..tools import ToolResult

def _provider_config(home: Path) -> ToolResult:
    from ..config import validate_config, load_config

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
                "routes": {
                    role: {
                        "provider": item.provider,
                        "kind": item.kind,
                        "model": item.model,
                        "api_base_url": item.api_base_url,
                        "has_api_key": bool(item.api_key),
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
                "routes": {},
                "validation_errors": [f"Failed to load config: {e}"],
            },
        )

