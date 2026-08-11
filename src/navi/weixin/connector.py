from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from navi.connector_registry import ConnectorAdapter, ConnectorSpec

from .config import load_weixin_config
from .store import WeixinStatusStore, WeixinStore


def _load_spec() -> ConnectorSpec:
    raw = yaml.safe_load(
        (Path(__file__).with_name("specs") / "connector.yaml").read_text(encoding="utf-8")
    )
    approval_commands = raw.get("approval_commands") or {}
    return ConnectorSpec(
        name=str(raw["name"]),
        surface=str(raw["surface"]),
        status_tool=str(raw["status_tool"]),
        status_description=str(raw["status_description"]),
        session_alias_prefix=str(raw["session_alias_prefix"]),
        local_source=str(raw["local_source"]),
        approval_approve_commands=tuple(
            str(item) for item in approval_commands.get("approve") or ()
        ),
        approval_reject_commands=tuple(
            str(item) for item in approval_commands.get("reject") or ()
        ),
    )


SPEC = _load_spec()


def create_adapter() -> ConnectorAdapter:
    from .evals import load_journey_eval_dataset, run_journey_eval_dataset

    return ConnectorAdapter(
        spec=SPEC,
        enabled=_enabled,
        status=_status,
        diagnostics=_diagnostics,
        register_tools=lambda registry, home: _register_tools(registry, home, SPEC),
        setup=_setup,
        run=_run,
        load_journey_eval_dataset=load_journey_eval_dataset,
        run_journey_eval_dataset=run_journey_eval_dataset,
    )


def _enabled(home: Path) -> bool:
    return load_weixin_config(home).enabled


def _status(home: Path) -> dict[str, Any]:
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
        **WeixinStatusStore(home).snapshot(),
    }
    return facts


def _diagnostics(home: Path) -> list[dict[str, str]]:
    config = load_weixin_config(home)
    saved_account = WeixinStore(home).load_account(config.account_id) if config.account_id else None
    token_present = bool(config.token or (saved_account and saved_account.token))
    ready = config.enabled and config.account_id and token_present
    health = WeixinStatusStore(home).snapshot()
    ingress_status = str(health.get("ingress_status") or "unknown")
    egress_status = str(health.get("egress_status") or "unknown")
    return [
        {
            "name": f"connector.{SPEC.name}.config",
            "status": "ok" if ready else "missing",
            "detail": (
                f"enabled={config.enabled} "
                f"account_present={bool(config.account_id)} "
                f"token_present={token_present}"
            ),
        },
        {
            "name": f"connector.{SPEC.name}.ingress",
            "status": (
                "ok"
                if ingress_status == "healthy"
                else "error"
                if ingress_status in {"fatal", "degraded", "stale"}
                else "warn"
            ),
            "detail": (
                f"ingress={ingress_status} "
                f"age_seconds={health.get('ingress_age_seconds', 0):.1f} "
                f"stale_after_seconds={health.get('ingress_stale_after_seconds', 0):.1f}"
            ),
        },
        {
            "name": f"connector.{SPEC.name}.egress",
            "status": (
                "ok"
                if egress_status == "healthy"
                else "error"
                if egress_status == "degraded"
                else "warn"
            ),
            "detail": (
                f"egress={egress_status} "
                f"reactive={health.get('reactive_egress_status', 'unknown')} "
                f"proactive={health.get('proactive_egress_status', 'unknown')} "
                f"incident={health.get('delivery_incident_status', 'unknown')} "
                f"rolling_7d={health.get('proactive_delivery_windows', {}).get('7d', {}).get('success_rate', 0):.4g} "
                "proactive_consecutive_failures="
                f"{health.get('consecutive_proactive_egress_failures', 0)} "
                f"last_provider_code={health.get('last_provider_code', '')}"
            ),
        },
    ]


def _register_tools(registry: Any, home: Path, spec: ConnectorSpec) -> None:
    from navi.tools import ALL_EXECUTION_CONTEXTS, ToolResult, ToolSpec

    registry.register(
        ToolSpec(
            name=spec.status_tool,
            capability_class="connector",
            execution_contexts=ALL_EXECUTION_CONTEXTS,
            description=spec.status_description,
            input_schema={"type": "object", "properties": {}},
            output_schema={
                "type": "object",
                "properties": {
                    "configured": {"type": "boolean"},
                    "account_id": {"type": "string"},
                    "saved_accounts": {"type": "array", "items": {"type": "string"}},
                    "dm_policy": {"type": "string"},
                    "group_policy": {"type": "string"},
                    "status": {"type": "string"},
                    "error": {"type": "string"},
                    "last_update": {"type": "number"},
                    "ingress_status": {"type": "string"},
                    "ingress_error": {"type": "string"},
                    "last_ingress_update": {"type": "number"},
                    "ingress_age_seconds": {"type": "number"},
                    "ingress_stale_after_seconds": {"type": "number"},
                    "egress_status": {"type": "string"},
                    "egress_error": {"type": "string"},
                    "last_egress_attempt_at": {"type": "number"},
                    "last_egress_success_at": {"type": "number"},
                    "consecutive_egress_failures": {"type": "integer"},
                    "consecutive_reactive_egress_failures": {"type": "integer"},
                    "consecutive_proactive_egress_failures": {"type": "integer"},
                    "reactive_egress_status": {"type": "string"},
                    "proactive_egress_status": {"type": "string"},
                    "reactive_egress_error": {"type": "string"},
                    "proactive_egress_error": {"type": "string"},
                    "last_reactive_egress_success_at": {"type": "number"},
                    "last_proactive_egress_success_at": {"type": "number"},
                    "proactive_circuit_open_until": {"type": "number"},
                    "last_provider_code": {"type": "string"},
                    "instantaneous_egress_status": {"type": "string"},
                    "delivery_incident_status": {"type": "string"},
                    "delivery_incident_windows": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "delivery_reliability_error": {"type": "string"},
                    "proactive_delivery_windows": {"type": "object"},
                },
            },
            source=spec.surface,
        ),
        lambda args: ToolResult(tool=spec.status_tool, ok=True, facts=_status(home)),
    )


async def _setup(home: Path, project_dir: Path, timeout_seconds: int, on_qr: Any | None) -> str:
    service = _service(home, project_dir)
    return await service.setup(timeout_seconds=timeout_seconds, on_qr=on_qr)


async def _run(home: Path, project_dir: Path, once: bool) -> None:
    await _service(home, project_dir).run(once=once)




def _service(home: Path, project_dir: Path):
    from .service import WeixinService

    return WeixinService(
        home=home,
        config=load_weixin_config(home),
        runtime=build_runtime_for_connector(home),
        project_dir=project_dir,
        local_source=SPEC.local_source,
        session_alias_prefix=SPEC.session_alias_prefix,
    )


def build_runtime_for_connector(home: Path):
    from navi.app_factory import build_runtime

    return build_runtime(home)
