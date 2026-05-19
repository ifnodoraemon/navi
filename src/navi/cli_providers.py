from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CliProviderSpec:
    name: str
    binary: str
    version_args: tuple[str, ...] = ("--version",)
    auth_status_args: tuple[str, ...] = ()
    auth_negative_markers: tuple[str, ...] = ()
    auth_detail: str = ""
    supports_execution: bool = False


CLI_PROVIDER_SPECS: tuple[CliProviderSpec, ...] = (
    CliProviderSpec(
        name="codex",
        binary="codex",
        auth_status_args=("login", "status"),
        auth_negative_markers=("not logged in", "error"),
        supports_execution=True,
    ),
    CliProviderSpec(
        name="gemini",
        binary="gemini",
        auth_detail="installed; auth is verified when a headless prompt runs",
    ),
)


def list_cli_provider_specs() -> tuple[CliProviderSpec, ...]:
    return CLI_PROVIDER_SPECS


def get_cli_provider_spec(name: str) -> CliProviderSpec | None:
    for spec in CLI_PROVIDER_SPECS:
        if spec.name == name:
            return spec
    return None
