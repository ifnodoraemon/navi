from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


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
        return [
            self._codex_status(),
            self._gemini_status(),
        ]

    def _codex_status(self) -> CliAuthStatus:
        path = shutil.which("codex") or ""
        if not path:
            return CliAuthStatus("codex", "", False, "", False, "codex not found on PATH")
        version = self._run([path, "--version"])
        login = self._run([path, "login", "status"])
        authenticated = "not logged in" not in login.lower() and "error" not in login.lower()
        return CliAuthStatus("codex", path, True, version.strip(), authenticated, login.strip())

    def _gemini_status(self) -> CliAuthStatus:
        path = shutil.which("gemini") or ""
        if not path:
            return CliAuthStatus("gemini", "", False, "", False, "gemini not found on PATH")
        version = self._run([path, "--version"])
        # Gemini CLI does not expose a stable non-interactive auth status command in this install.
        return CliAuthStatus(
            "gemini",
            path,
            True,
            version.strip(),
            True,
            "installed; auth is verified when a headless prompt runs",
        )

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
