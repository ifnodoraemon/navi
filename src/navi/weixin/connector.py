from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from navi.connector_registry import ConnectorAdapter, ConnectorSpec

from .config import load_weixin_config
from .store import WeixinStore


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
        register_tools=lambda registry, home: _register_tools(registry, home, SPEC),
        setup=_setup,
        run=_run,
    )


def _enabled(home: Path) -> bool:
    return load_weixin_config(home).enabled


def _status(home: Path) -> dict[str, Any]:
    import json
    config = load_weixin_config(home)
    store = WeixinStore(home)
    facts = {
        "configured": bool(config.account_id or store.list_accounts()),
        "account_id": config.account_id,
        "saved_accounts": store.list_accounts(),
        "dm_policy": config.dm_policy,
        "group_policy": config.group_policy,
        "status": "unknown",
        "error": "",
        "last_update": 0.0,
    }
    status_file = home / "weixin" / "status.json"
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


async def _setup(home: Path, timeout_seconds: int, on_qr: Any | None) -> str:
    service = _service(home)
    return await service.setup(timeout_seconds=timeout_seconds, on_qr=on_qr)


async def _run(home: Path, once: bool) -> None:
    await _service(home).run(once=once)


def _service(home: Path):
    from .service import WeixinService

    return WeixinService(
        home=home,
        config=load_weixin_config(home),
        runtime=build_runtime_for_connector(home),
        local_source=SPEC.local_source,
        session_alias_prefix=SPEC.session_alias_prefix,
    )


def build_runtime_for_connector(home: Path):
    from navi.app_factory import build_runtime

    return build_runtime(home)
