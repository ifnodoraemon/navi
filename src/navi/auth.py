from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

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
        binary_path = shutil.which(spec.binary)
        if not binary_path:
            return CliAuthStatus(
                spec.name,
                "",
                False,
                "",
                False,
                f"binary not found: {spec.binary}",
            )
        version = _command_output([binary_path, *spec.version_args]) if spec.version_args else ""
        authenticated = any(Path(path).expanduser().exists() for path in spec.auth_files)
        detail = spec.auth_detail
        if spec.auth_status_args:
            auth_result = _command_output(
                [binary_path, *spec.auth_status_args], include_returncode=True
            )
            negative = any(
                marker.lower() in auth_result.lower() for marker in spec.auth_negative_markers
            )
            authenticated = authenticated or ("returncode=0" in auth_result and not negative)
        if not authenticated and not detail:
            detail = "authentication not detected"
        return CliAuthStatus(
            spec.name,
            binary_path,
            True,
            version,
            authenticated,
            detail,
        )


def _command_output(command: list[str], *, include_returncode: bool = False) -> str:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"error={exc.__class__.__name__}" if include_returncode else ""
    output = " ".join((result.stdout or result.stderr or "").split())
    if include_returncode:
        return f"returncode={result.returncode} {output}".strip()
    return output[:120]
