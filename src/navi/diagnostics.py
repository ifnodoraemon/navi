from __future__ import annotations

import shutil
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from .auth import AuthInspector
from .capabilities import build_capability_registry
from .config import load_config, validate_config
from .connector_registry import load_connector_adapters
from .fact_tools import service_facts
from .provider import resolve_model_config
from .provider_specs import get_provider_spec
from .service import systemd_user_unit_path
from .telegram.config import load_telegram_config
from .weixin.config import load_weixin_config
from .weixin.store import WeixinStore


@dataclass(frozen=True)
class DiagnosticCheck:
    name: str
    status: str
    detail: str = ""


def run_diagnostics(
    home: Path,
    *,
    project_dir: Path,
    include_connectivity: bool = False,
) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []
    config = load_config(home)
    validation_errors = validate_config(config, home)
    checks.append(_check_path("home", home, required=True))
    checks.append(_check_path("config", home / "config.yaml", required=True))
    checks.append(
        DiagnosticCheck(
            "config.validation",
            "ok" if not validation_errors else "error",
            "; ".join(validation_errors),
        )
    )
    checks.extend(_state_checks(home))
    checks.extend(_external_tool_checks(("git", "rg", "systemctl", "node", "npx")))
    checks.extend(_browser_dependency_checks())
    checks.extend(_computer_use_checks())
    checks.extend(_connector_status_file_checks(home))
    checks.extend(_connector_config_checks(home))
    checks.extend(_api_config_checks(config))
    if include_connectivity:
        checks.extend(_api_connectivity_checks(config))
    unit = systemd_user_unit_path(config.runtime.service_name)
    checks.append(_check_path("service.unit", unit, required=False))
    checks.append(_service_runtime_check(config.runtime.service_name))
    tools = build_capability_registry(home, project_dir=project_dir).list_specs()
    checks.append(DiagnosticCheck("capabilities", "ok" if tools else "error", f"{len(tools)} registered"))
    for item in AuthInspector().status():
        status = "ok" if item.installed and item.authenticated else "warn"
        if not item.installed:
            status = "missing"
        detail = item.detail or item.version
        checks.append(DiagnosticCheck(f"auth.{item.name}", status, detail))
    return checks


def _state_checks(home: Path) -> list[DiagnosticCheck]:
    checks = [_check_path("skills.dir", home / "skills", required=False)]
    for filename in (
        "memory.db",
        "runs.db",
        "goals.db",
        "trust.db",
        "traces.db",
        "evolution.db",
        "subagents.db",
    ):
        checks.append(_check_path(f"state.{filename}", home / filename, required=False))
    return checks


def _external_tool_checks(names: tuple[str, ...]) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []
    for name in names:
        path = shutil.which(name)
        checks.append(DiagnosticCheck(f"tool.{name}", "ok" if path else "missing", path or "not found"))
    return checks


def _browser_dependency_checks() -> list[DiagnosticCheck]:
    checks = []
    playwright = shutil.which("playwright")
    checks.append(DiagnosticCheck("browser.playwright", "ok" if playwright else "missing", playwright or "not found"))
    chromium = _first_existing_path(
        (
            Path.home() / ".cache" / "ms-playwright",
            Path.home() / ".cache" / "ms-playwright-go",
        )
    )
    checks.append(
        DiagnosticCheck(
            "browser.chromium",
            "ok" if chromium else "missing",
            str(chromium) if chromium else "playwright browser cache not found",
        )
    )
    return checks


def _computer_use_checks() -> list[DiagnosticCheck]:
    display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    checks = [
        DiagnosticCheck("computer.display", "ok" if display else "missing", display or "DISPLAY/WAYLAND_DISPLAY not set")
    ]
    for name in ("xdotool", "scrot", "gnome-screenshot"):
        path = shutil.which(name)
        checks.append(DiagnosticCheck(f"computer.tool.{name}", "ok" if path else "missing", path or "not found"))
    return checks


def _connector_config_checks(home: Path) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []
    telegram_name = "telegram"
    telegram = load_telegram_config(home)
    telegram_status = "ok" if telegram.enabled and telegram.bot_token else "missing"
    if not telegram.enabled:
        telegram_status = "missing"
    checks.append(
        DiagnosticCheck(
            f"connector.{telegram_name}.config",
            telegram_status,
            f"enabled={telegram.enabled} token_present={bool(telegram.bot_token)} home_chat={bool(telegram.home_chat_id)}",
        )
    )
    weixin_name = "weixin"
    weixin = load_weixin_config(home)
    saved_account = WeixinStore(home).load_account(weixin.account_id) if weixin.account_id else None
    token_present = bool(weixin.token or (saved_account and saved_account.token))
    weixin_ready = weixin.enabled and weixin.account_id and token_present
    checks.append(
        DiagnosticCheck(
            f"connector.{weixin_name}.config",
            "ok" if weixin_ready else "missing",
            f"enabled={weixin.enabled} account_present={bool(weixin.account_id)} token_present={token_present}",
        )
    )
    return checks


def _connector_status_file_checks(home: Path) -> list[DiagnosticCheck]:
    return [
        _check_path(f"connector.{adapter.name}.status_file", home / adapter.name / "status.json", required=False)
        for adapter in load_connector_adapters()
    ]


def _api_config_checks(config) -> list[DiagnosticCheck]:
    checks = []
    if config.model.kind == "mock":
        checks.append(DiagnosticCheck("api.model.config", "ok", "mock provider"))
        return checks
    if not config.model.api_base_url:
        checks.append(DiagnosticCheck("api.model.config", "error", "api_base_url missing"))
    elif not config.model.api_key:
        checks.append(DiagnosticCheck("api.model.config", "error", "api_key missing"))
    else:
        checks.append(
            DiagnosticCheck(
                "api.model.config",
                "ok",
                f"{config.model.provider} {config.model.api_base_url} key_present=True",
            )
        )
    return checks


def _api_connectivity_checks(config) -> list[DiagnosticCheck]:
    if config.model.kind == "mock":
        return [DiagnosticCheck("api.model.connectivity", "ok", "mock provider")]
    if not config.model.api_base_url or not config.model.api_key:
        return [DiagnosticCheck("api.model.connectivity", "warn", "skipped: model API config incomplete")]
    try:
        resolved = resolve_model_config(config.model)
        spec = get_provider_spec(resolved.provider)
        if spec.kind == "anthropic-compatible":
            response = httpx.post(
                f"{resolved.api_base_url}/messages",
                headers={
                    "x-api-key": resolved.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": resolved.model,
                    "max_tokens": 8,
                    "messages": [{"role": "user", "content": "health check"}],
                },
                timeout=5.0,
            )
        else:
            response = httpx.post(
                f"{resolved.api_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {resolved.api_key}"},
                json={
                    "model": resolved.model,
                    "messages": [{"role": "user", "content": "health check"}],
                    "temperature": 0,
                    "max_tokens": 8,
                },
                timeout=5.0,
            )
        response.raise_for_status()
    except Exception as exc:
        return [DiagnosticCheck("api.model.connectivity", "warn", f"{exc.__class__.__name__}")]
    return [DiagnosticCheck("api.model.connectivity", "ok", "chat completion succeeded")]


def _first_existing_path(paths: tuple[Path, ...]) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def _service_runtime_check(name: str) -> DiagnosticCheck:
    try:
        facts = service_facts(name)
    except Exception as exc:
        return DiagnosticCheck("service.runtime", "warn", f"{exc.__class__.__name__}")
    active = facts.properties.get("ActiveState") or "unknown"
    substate = facts.properties.get("SubState") or "unknown"
    if facts.exit_code == 0 and active == "active":
        return DiagnosticCheck("service.runtime", "ok", f"{name} {active}/{substate}")
    fallback = _systemd_is_active(name)
    if fallback:
        return DiagnosticCheck("service.runtime", "ok", f"{name} {fallback}/running")
    return DiagnosticCheck("service.runtime", "warn", f"{name} {active}/{substate} exit_code={facts.exit_code}")


def _systemd_is_active(name: str) -> str:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception:
        return ""
    state = result.stdout.strip()
    return state if result.returncode == 0 and state == "active" else ""


def _check_path(name: str, path: Path, *, required: bool) -> DiagnosticCheck:
    if path.exists():
        return DiagnosticCheck(name, "ok", str(path))
    return DiagnosticCheck(name, "error" if required else "missing", str(path))
