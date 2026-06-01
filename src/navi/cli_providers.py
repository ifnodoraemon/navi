from __future__ import annotations

from dataclasses import dataclass

from .spec_loader import load_spec


@dataclass(frozen=True)
class CliProviderSpec:
    name: str
    binary: str
    version_args: tuple[str, ...] = ()
    auth_status_args: tuple[str, ...] = ()
    auth_negative_markers: tuple[str, ...] = ()
    auth_files: tuple[str, ...] = ()
    auth_detail: str = ""
    supports_execution: bool = False


CLI_PROVIDER_SPECS: tuple[CliProviderSpec, ...] = tuple(
    CliProviderSpec(
        name=str(item["name"]),
        binary=str(item["binary"]),
        version_args=tuple(item.get("version_args") or ()),
        auth_status_args=tuple(item.get("auth_status_args") or ()),
        auth_negative_markers=tuple(item.get("auth_negative_markers") or ()),
        auth_files=tuple(item.get("auth_files") or ()),
        auth_detail=str(item.get("auth_detail") or ""),
        supports_execution=bool(item.get("supports_execution", False)),
    )
    for item in load_spec("cli_providers.yaml")
)


def list_cli_provider_specs() -> tuple[CliProviderSpec, ...]:
    return CLI_PROVIDER_SPECS


def get_cli_provider_spec(name: str) -> CliProviderSpec | None:
    for spec in CLI_PROVIDER_SPECS:
        if spec.name == name:
            return spec
    return None
