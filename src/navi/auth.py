from __future__ import annotations

import shutil
import subprocess
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
        path = shutil.which(spec.binary) or ""
        if not path:
            return CliAuthStatus(spec.name, "", False, "", False, f"{spec.binary} not found on PATH")
        version = self._run([path, *spec.version_args])
        if not spec.auth_status_args:
            return CliAuthStatus(spec.name, path, True, version.strip(), True, spec.auth_detail)
        auth = self._run([path, *spec.auth_status_args])
        lowered = auth.lower()
        authenticated = not any(marker in lowered for marker in spec.auth_negative_markers)
        return CliAuthStatus(spec.name, path, True, version.strip(), authenticated, auth.strip())

    @staticmethod
    def _run(command: list[str]) -> str:
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
        except Exception as exc:  # pragma: no cover - exact platform errors vary.
            return str(exc)
        return (result.stdout or result.stderr or "").strip()
