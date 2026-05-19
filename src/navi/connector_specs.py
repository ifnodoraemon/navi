from __future__ import annotations

from dataclasses import dataclass

from .spec_loader import load_spec


@dataclass(frozen=True)
class SessionCommandSpec:
    command: str
    affordance: str
    actions: dict[str, str]
    usage: str


@dataclass(frozen=True)
class ConnectorSpec:
    name: str
    surface: str
    status_tool: str
    status_description: str
    session_alias_prefix: str
    local_source: str
    session_command: SessionCommandSpec


def get_connector_spec(name: str) -> ConnectorSpec:
    raw = load_spec("connectors.yaml")[name]
    return _parse_connector_spec(raw)


def list_connector_specs() -> list[ConnectorSpec]:
    return [_parse_connector_spec(raw) for raw in load_spec("connectors.yaml").values()]


def _parse_connector_spec(raw: dict) -> ConnectorSpec:
    session_raw = raw["session_command"]
    return ConnectorSpec(
        name=str(raw["name"]),
        surface=str(raw["surface"]),
        status_tool=str(raw["status_tool"]),
        status_description=str(raw["status_description"]),
        session_alias_prefix=str(raw["session_alias_prefix"]),
        local_source=str(raw["local_source"]),
        session_command=SessionCommandSpec(
            command=str(session_raw["command"]),
            affordance=str(session_raw["affordance"]),
            actions={
                str(action): str(detail["operation"])
                for action, detail in (session_raw.get("actions") or {}).items()
            },
            usage=str(session_raw["usage"]),
        ),
    )
