from __future__ import annotations

import asyncio
import shutil
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from .auth import AuthInspector
from .capabilities import build_capability_registry
from .config import load_config, load_runtime_env, validate_config
from .connector_registry import ConnectorAdapter, load_connector_adapters
from .provider import resolve_model_config
from .provider_specs import get_provider_spec
from .service import systemd_user_unit_path


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
    connector_adapters = load_connector_adapters()
    checks.extend(_connector_status_file_checks(home, connector_adapters))
    checks.extend(_connector_config_checks(home, connector_adapters))
    checks.extend(_api_config_checks(config))
    checks.extend(_search_config_checks(home))
    checks.extend(_mcp_config_checks(home))
    if include_connectivity:
        checks.extend(_api_connectivity_checks(config))
        checks.append(_search_connectivity_check(home))
    unit = systemd_user_unit_path(config.runtime.service_name)
    checks.append(_check_path("service.unit", unit, required=False))
    checks.append(_service_runtime_check(config.runtime.service_name))
    tools = build_capability_registry(home, project_dir=project_dir).list_specs()
    checks.append(
        DiagnosticCheck("capabilities", "ok" if tools else "error", f"{len(tools)} registered")
    )
    from .metrics import MetricsProjector

    snapshot = MetricsProjector(home).snapshot()
    for slo in snapshot.slos:
        status = {
            "met": "ok",
            "breached": "error",
            "insufficient_data": "warn",
        }[slo.status]
        checks.append(
            DiagnosticCheck(
                f"slo.{slo.name}",
                status,
                f"actual={slo.actual:.4g} target={slo.target} samples={slo.samples}",
            )
        )
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
        "traces.db",
        "evolution.db",
    ):
        checks.append(_check_path(f"state.{filename}", home / filename, required=False))
    return checks


def _external_tool_checks(names: tuple[str, ...]) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []
    for name in names:
        path = shutil.which(name)
        checks.append(
            DiagnosticCheck(f"tool.{name}", "ok" if path else "missing", path or "not found")
        )
    return checks


def _browser_dependency_checks() -> list[DiagnosticCheck]:
    checks = []
    playwright = shutil.which("playwright")
    checks.append(
        DiagnosticCheck(
            "browser.playwright", "ok" if playwright else "missing", playwright or "not found"
        )
    )
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
        DiagnosticCheck(
            "computer.display",
            "ok" if display else "missing",
            display or "DISPLAY/WAYLAND_DISPLAY not set",
        )
    ]
    for name in ("xdotool", "scrot", "gnome-screenshot"):
        path = shutil.which(name)
        checks.append(
            DiagnosticCheck(
                f"computer.tool.{name}", "ok" if path else "missing", path or "not found"
            )
        )
    return checks


def _connector_config_checks(home: Path, adapters: list[ConnectorAdapter]) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []
    for adapter in adapters:
        if adapter.diagnostics is None:
            continue
        for item in adapter.diagnostics(home):
            checks.append(
                DiagnosticCheck(
                    str(item.get("name") or f"connector.{adapter.name}.diagnostics"),
                    str(item.get("status") or "missing"),
                    str(item.get("detail") or ""),
                )
            )
    return checks


def _connector_status_file_checks(
    home: Path, adapters: list[ConnectorAdapter]
) -> list[DiagnosticCheck]:
    return [
        _check_path(
            f"connector.{adapter.name}.status_file",
            home / adapter.name / "status.json",
            required=False,
        )
        for adapter in adapters
    ]


def _api_config_checks(config) -> list[DiagnosticCheck]:
    checks = []
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


def _search_config_checks(home: Path) -> list[DiagnosticCheck]:
    from .core_tools.web_search import _searxng_endpoints
    from .mcp_client import DEFAULT_EXA_MCP_URL

    env = load_runtime_env(home)
    provider = str(env.get("NAVI_WEB_SEARCH_PROVIDER") or "auto").strip().lower()
    if provider in {"searxng", "searxng_json"} and not _searxng_endpoints(env):
        return [
            DiagnosticCheck(
                "search.config",
                "error",
                "SearXNG selected but NAVI_WEB_SEARCH_SEARXNG_URL(S) is missing",
            )
        ]
    if provider not in {"auto", "searxng", "searxng_json", "exa", "exa_mcp", "mcp"}:
        return [
            DiagnosticCheck("search.config", "error", f"unsupported provider {provider}")
        ]
    if provider in {"searxng", "searxng_json"}:
        detail = f"provider=searxng endpoints={len(_searxng_endpoints(env))}"
    else:
        exa_url = str(env.get("NAVI_WEB_SEARCH_EXA_MCP_URL") or DEFAULT_EXA_MCP_URL)
        detail = (
            f"provider={provider} fallback=exa_mcp endpoint={exa_url.split('?', 1)[0]} "
            f"api_key_present={bool(env.get('NAVI_EXA_API_KEY') or env.get('EXA_API_KEY'))}"
        )
    return [DiagnosticCheck("search.config", "ok", detail)]


def _mcp_config_checks(home: Path) -> list[DiagnosticCheck]:
    from .mcp_tools import load_mcp_config

    report = load_mcp_config(home)
    if report.errors:
        return [DiagnosticCheck("mcp.config", "error", "; ".join(report.errors))]
    if not report.path.exists():
        return [
            DiagnosticCheck(
                "mcp.config",
                "ok",
                f"built-in servers={len(report.servers)}; optional {report.path} not found",
            )
        ]
    return [
        DiagnosticCheck(
            "mcp.config",
            "ok",
            f"{len(report.servers)} enabled server(s) in {report.path}",
        )
    ]


def _search_connectivity_check(home: Path) -> DiagnosticCheck:
    from .core_tools.web_search import _web_search

    try:
        result = asyncio.run(
            _web_search({"query": "Navi web search connectivity check", "limit": 1}, home=home)
        )
    except Exception as exc:  # pragma: no cover - defensive diagnostic boundary.
        return DiagnosticCheck("search.connectivity", "error", str(exc))
    if result.ok:
        return DiagnosticCheck(
            "search.connectivity",
            "ok",
            f"provider={result.facts.get('provider')} results={len(result.facts.get('results') or [])}",
        )
    return DiagnosticCheck(
        "search.connectivity",
        "error",
        f"{result.facts.get('error_reason')}: {result.error}",
    )


def _api_connectivity_checks(config) -> list[DiagnosticCheck]:
    if not config.model.api_base_url or not config.model.api_key:
        return [
            DiagnosticCheck(
                "api.model.connectivity", "warn", "skipped: model API config incomplete"
            )
        ]
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


def _service_facts(name: str) -> dict[str, object]:
    """Return ``{properties, exit_code, stderr}`` from ``systemctl --user show``.

    Inlined from the removed ``fact_tools.service_facts``; this is the only
    remaining caller of that parsing logic."""
    if shutil.which("systemctl") is None:
        return {
            "properties": {},
            "exit_code": 127,
            "stderr": "systemctl not found; service manager unavailable on this OS",
        }
    command = [
        "systemctl",
        "--user",
        "show",
        name,
        "--property=ActiveEnterTimestamp",
        "--property=ActiveState",
        "--property=SubState",
        "--property=MainPID",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=8)
    properties: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key:
            properties[key] = value
    if result.returncode != 0:
        fb = subprocess.run(
            ["systemctl", "--user", "is-active", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
        active_state = fb.stdout.strip()
        if fb.returncode == 0 and active_state:
            properties["ActiveState"] = active_state
            properties.setdefault("SubState", active_state)
            return {
                "properties": properties,
                "exit_code": 0,
                "stderr": result.stderr.strip(),
            }
    return {
        "properties": properties,
        "exit_code": result.returncode,
        "stderr": result.stderr.strip(),
    }


def _service_runtime_check(name: str) -> DiagnosticCheck:
    try:
        facts = _service_facts(name)
    except Exception as exc:
        return DiagnosticCheck("service.runtime", "warn", f"{exc.__class__.__name__}")
    raw_properties = facts["properties"]
    properties = raw_properties if isinstance(raw_properties, dict) else {}
    active = properties.get("ActiveState") or "unknown"
    substate = properties.get("SubState") or "unknown"
    if facts["exit_code"] == 0 and active == "active":
        return DiagnosticCheck("service.runtime", "ok", f"{name} {active}/{substate}")
    fallback = _systemd_is_active(name)
    if fallback:
        return DiagnosticCheck("service.runtime", "ok", f"{name} {fallback}/running")
    return DiagnosticCheck(
        "service.runtime", "warn", f"{name} {active}/{substate} exit_code={facts['exit_code']}"
    )


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
