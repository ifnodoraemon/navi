from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from navi.connector_registry import ConnectorAdapter, ConnectorSpec

from .config import load_telegram_config


def _load_spec() -> ConnectorSpec:
    raw = yaml.safe_load((Path(__file__).with_name("specs") / "connector.yaml").read_text(encoding="utf-8"))
    return ConnectorSpec(
        name=str(raw["name"]),
        surface=str(raw["surface"]),
        status_tool=str(raw["status_tool"]),
        status_description=str(raw["status_description"]),
        session_alias_prefix=str(raw["session_alias_prefix"]),
        local_source=str(raw["local_source"]),
        approval_template=str(raw.get("approval_template") or ""),
        approval_commands=dict(raw.get("approval_commands") or {}),
    )


SPEC = _load_spec()


def create_adapter() -> ConnectorAdapter:
    return ConnectorAdapter(
        spec=SPEC,
        enabled=_enabled,
        status=_status,
        diagnostics=_diagnostics,
        register_tools=lambda registry, home: _register_tools(registry, home, SPEC),
        run=_run,
    )


def _enabled(home: Path) -> bool:
    return load_telegram_config(home).enabled


def _status(home: Path) -> dict[str, Any]:
    import json
    config = load_telegram_config(home)
    facts = {
        "configured": bool(config.bot_token),
        "dm_policy": config.dm_policy,
        "home_chat_id": config.home_chat_id,
        "allowed_users_count": len(config.allowed_users),
        "status": "unknown",
        "error": "",
        "last_update": 0.0,
    }
    status_file = home / "telegram" / "status.json"
    if status_file.exists():
        try:
            data = json.loads(status_file.read_text(encoding="utf-8"))
            facts.update({
                "status": data.get("status", "unknown"),
                "error": data.get("error", ""),
                "last_update": data.get("last_update", 0.0),
            })
        except Exception:
            pass
    return facts


def _diagnostics(home: Path) -> list[dict[str, str]]:
    config = load_telegram_config(home)
    status = "ok" if config.enabled and config.bot_token else "missing"
    return [
        {
            "name": f"connector.{SPEC.name}.config",
            "status": status,
            "detail": (
                f"enabled={config.enabled} "
                f"token_present={bool(config.bot_token)} "
                f"home_chat={bool(config.home_chat_id)}"
            ),
        }
    ]


def _register_tools(registry: Any, home: Path, spec: ConnectorSpec) -> None:
    from navi.tools import ToolResult, ToolSpec

    registry.register(
        ToolSpec(
            name=spec.status_tool,
            description=spec.status_description,
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "object"},
            source=spec.surface,
        ),
        lambda args: ToolResult(tool=spec.status_tool, ok=True, facts=_status(home)),
    )


async def _run(home: Path, project_dir: Path, once: bool) -> None:
    await _service(home, project_dir).run(once=once)


def _service(home: Path, project_dir: Path):
    from navi.app_factory import build_runtime

    from .service import TelegramService

    return TelegramService(
        home=home,
        config=load_telegram_config(home),
        runtime=build_runtime(home),
        project_dir=project_dir,
        local_source=SPEC.local_source,
        session_alias_prefix=SPEC.session_alias_prefix,
    )
