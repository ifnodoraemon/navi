from __future__ import annotations

from dataclasses import dataclass

from .cli_providers import CliProviderSpec, list_cli_provider_specs


@dataclass(frozen=True)
class CliAuthStatus:
    name: str
    path: str
    installed: bool
    version: str
    authenticated: bool
    detail: str


class AuthInspector:
    def status(self) -> list[CliAuthStatus]:
        return [self._status_for(spec) for spec in list_cli_provider_specs()]

    def _status_for(self, spec: CliProviderSpec) -> CliAuthStatus:
        return CliAuthStatus(
            spec.name,
            "",
            False,
            "",
            False,
            "external CLI providers are disabled; Navi uses internal execution",
        )
