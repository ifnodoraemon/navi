from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .defaults import (
    DEFAULT_API_HOST,
    DEFAULT_API_PORT,
    DEFAULT_EXA_MCP_URL,
    DEFAULT_EXECUTION_PROVIDER,
    DEFAULT_EXECUTION_TIMEOUT_SECONDS,
    DEFAULT_LOCAL_SURFACE,
    DEFAULT_MODEL_TIMEOUT_SECONDS,
    DEFAULT_MODEL_MODEL,
    DEFAULT_MODEL_PROVIDER,
    DEFAULT_MODEL_ROLE_PARAMS,
    DEFAULT_SEARCH_MCP_SERVER,
    DEFAULT_SERVICE_NAME,
    DEFAULT_TELEGRAM_API_BASE_URL,
    DEFAULT_TELEGRAM_DM_POLICY,
    DEFAULT_TELEGRAM_ENABLED,
    DEFAULT_WEIXIN_BASE_URL,
    DEFAULT_WEIXIN_CDN_BASE_URL,
    DEFAULT_WEIXIN_DM_POLICY,
    DEFAULT_WEIXIN_ENABLED,
    DEFAULT_WEIXIN_GROUP_POLICY,
)
from .paths import ensure_home
from .provider_specs import get_provider_spec
from .provider_specs import ProviderSpec

_MODEL_FIELDS = {
    "provider",
    "model",
    "api_base_url",
    "api_key",
    "kind",
    "timeout_seconds",
    "response_transport",
    "request_options",
    "routes",
    "role_params",
}


RESERVED_REQUEST_OPTIONS = frozenset(
    {
        "model",
        "messages",
        "system",
        "temperature",
        "max_tokens",
        "response_format",
        "stream",
        "stream_options",
        "tools",
        "tool_choice",
    }
)


@dataclass
class ModelConfig:
    provider: str = DEFAULT_MODEL_PROVIDER
    model: str = DEFAULT_MODEL_MODEL
    api_base_url: str = ""
    api_key: str = ""
    kind: str = ""
    timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS
    response_transport: str = "json"
    request_options: dict[str, Any] = field(default_factory=dict)
    routes: dict[str, "ModelConfig"] = field(default_factory=dict)
    role_params: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get_role_params(self, role: str) -> dict[str, Any]:
        base = dict(
            DEFAULT_MODEL_ROLE_PARAMS.get(role) or DEFAULT_MODEL_ROLE_PARAMS["default"]
        )
        overrides = self.role_params.get(role, {})
        if isinstance(overrides, dict):
            base.update(overrides)
        return base


@dataclass
class RuntimeConfig:
    service_name: str = DEFAULT_SERVICE_NAME
    local_surface: str = DEFAULT_LOCAL_SURFACE


@dataclass
class ExecutionConfig:
    provider: str = DEFAULT_EXECUTION_PROVIDER
    timeout_seconds: float = DEFAULT_EXECUTION_TIMEOUT_SECONDS


@dataclass
class ApiConfig:
    host: str = DEFAULT_API_HOST
    port: int = DEFAULT_API_PORT
    api_key: str = ""


@dataclass
class SearchProviderConfig:
    kind: str
    enabled: bool = True
    endpoint: str = ""
    allow_private_network: bool = False
    mcp_server: str = ""
    bearer_token: str = ""
    categories: str = ""
    language: str = ""
    time_range: str = ""


def _default_search_providers() -> dict[str, SearchProviderConfig]:
    return {
        "exa": SearchProviderConfig(
            kind="exa_mcp",
            mcp_server=DEFAULT_SEARCH_MCP_SERVER,
        ),
        "searxng": SearchProviderConfig(
            kind="searxng",
            enabled=False,
        ),
        "x": SearchProviderConfig(
            kind="x_api",
            enabled=False,
            endpoint="https://api.x.com",
        ),
    }


@dataclass
class SearchConfig:
    providers: dict[str, SearchProviderConfig] = field(
        default_factory=_default_search_providers
    )


def _default_connectors() -> dict[str, dict[str, Any]]:
    return {
        "telegram": {
            "enabled": DEFAULT_TELEGRAM_ENABLED,
            "bot_token": "",
            "api_base_url": DEFAULT_TELEGRAM_API_BASE_URL,
            "dm_policy": DEFAULT_TELEGRAM_DM_POLICY,
            "allowed_users": [],
            "home_chat_id": "",
        },
        "weixin": {
            "enabled": DEFAULT_WEIXIN_ENABLED,
            "account_id": "",
            "token": "",
            "base_url": DEFAULT_WEIXIN_BASE_URL,
            "cdn_base_url": DEFAULT_WEIXIN_CDN_BASE_URL,
            "dm_policy": DEFAULT_WEIXIN_DM_POLICY,
            "allowed_users": [],
            "group_policy": DEFAULT_WEIXIN_GROUP_POLICY,
            "group_allowed_users": [],
            "home_channel": "",
        },
    }


def _default_mcp_servers() -> dict[str, dict[str, Any]]:
    return {
        "exa": {
            "transport": "streamable_http",
            "url": DEFAULT_EXA_MCP_URL,
            "headers": {},
            "tool_permissions": {
                "web_search_exa": "network",
                "web_fetch_exa": "network",
            },
            "enabled": True,
        }
    }


@dataclass
class NaviConfig:
    model: ModelConfig = field(default_factory=ModelConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    connectors: dict[str, dict[str, Any]] = field(default_factory=_default_connectors)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=_default_mcp_servers)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config root must be a mapping: {path}")
    return data


def load_config(home: Path | None = None) -> NaviConfig:
    home = home or ensure_home()
    legacy_paths = [home / name for name in ("env", "mcp.json", "api_key")]
    present_legacy = [path.name for path in legacy_paths if path.exists()]
    if present_legacy:
        raise ValueError(
            "legacy configuration files are unsupported; move their values into config.yaml: "
            + ", ".join(present_legacy)
        )
    raw = _read_yaml(home / "config.yaml")
    allowed_sections = {"model", "runtime", "execution", "api", "search", "connectors", "mcp"}
    unknown_sections = sorted(set(raw) - allowed_sections)
    if unknown_sections:
        raise ValueError(f"unsupported top-level config sections: {', '.join(unknown_sections)}")

    model_raw = _mapping(raw.get("model"), "model")
    runtime_raw = _mapping(raw.get("runtime"), "runtime")
    execution_raw = _mapping(raw.get("execution"), "execution")
    api_raw = _mapping(raw.get("api"), "api")
    search_raw = _mapping(raw.get("search"), "search")
    connectors_raw = _mapping(raw.get("connectors"), "connectors")
    mcp_raw = _mapping(raw.get("mcp"), "mcp")
    _reject_unknown(runtime_raw, {"service_name", "local_surface"}, "runtime")
    _reject_unknown(execution_raw, {"provider", "timeout_seconds"}, "execution")
    _reject_unknown(api_raw, {"host", "port", "api_key"}, "api")
    _reject_unknown(search_raw, {"providers"}, "search")
    model = _model_config(model_raw)

    runtime = RuntimeConfig(
        service_name=str(runtime_raw.get("service_name", DEFAULT_SERVICE_NAME)).strip(),
        local_surface=str(runtime_raw.get("local_surface", DEFAULT_LOCAL_SURFACE)).strip(),
    )
    execution = ExecutionConfig(
        provider=str(execution_raw.get("provider", DEFAULT_EXECUTION_PROVIDER)).strip()
        or DEFAULT_EXECUTION_PROVIDER,
        timeout_seconds=_positive_float(
            execution_raw.get("timeout_seconds", DEFAULT_EXECUTION_TIMEOUT_SECONDS),
            "execution.timeout_seconds",
        ),
    )
    api = ApiConfig(
        host=str(api_raw.get("host", DEFAULT_API_HOST)).strip(),
        port=_port(api_raw.get("port", DEFAULT_API_PORT), "api.port"),
        api_key=str(api_raw.get("api_key") or "").strip(),
    )
    search_providers_raw = _mapping(search_raw.get("providers"), "search.providers")
    search = SearchConfig(
        providers=(
            {
                _search_provider_name(name): _search_provider_config(
                    _mapping(item, f"search.providers.{name}"),
                    path=f"search.providers.{name}",
                )
                for name, item in search_providers_raw.items()
            }
            if "providers" in search_raw
            else _default_search_providers()
        )
    )
    connectors = _default_connectors()
    for name, item in connectors_raw.items():
        connector_name = str(name)
        connector_raw = _mapping(item, f"connectors.{connector_name}")
        connectors[connector_name] = {
            **connectors.get(connector_name, {}),
            **connector_raw,
        }
    mcp_servers_raw = _mapping(mcp_raw.get("servers"), "mcp.servers")
    mcp_servers = (
        {str(name): _mapping(item, f"mcp.servers.{name}") for name, item in mcp_servers_raw.items()}
        if "servers" in mcp_raw
        else _default_mcp_servers()
    )
    unknown_mcp = sorted(set(mcp_raw) - {"servers"})
    if unknown_mcp:
        raise ValueError(f"unsupported mcp config fields: {', '.join(unknown_mcp)}")
    return NaviConfig(
        model=model,
        runtime=runtime,
        execution=execution,
        api=api,
        search=search,
        connectors=connectors,
        mcp_servers=mcp_servers,
    )


def write_default_config(home: Path | None = None) -> Path:
    home = home or ensure_home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.yaml"
    if path.exists():
        return path
    path.write_text(
        yaml.safe_dump(
            {
                "model": {
                    "provider": DEFAULT_MODEL_PROVIDER,
                    "model": DEFAULT_MODEL_MODEL,
                    "timeout_seconds": DEFAULT_MODEL_TIMEOUT_SECONDS,
                    "response_transport": "json",
                    "request_options": {},
                },
                "runtime": {
                    "service_name": DEFAULT_SERVICE_NAME,
                    "local_surface": DEFAULT_LOCAL_SURFACE,
                },
                "execution": {
                    "provider": DEFAULT_EXECUTION_PROVIDER,
                    "timeout_seconds": DEFAULT_EXECUTION_TIMEOUT_SECONDS,
                },
                "api": {
                    "host": DEFAULT_API_HOST,
                    "port": DEFAULT_API_PORT,
                    "api_key": secrets.token_hex(32),
                },
                "search": {
                    "providers": {
                        "exa": {
                            "kind": "exa_mcp",
                            "enabled": True,
                            "mcp_server": DEFAULT_SEARCH_MCP_SERVER,
                        },
                        "searxng": {
                            "kind": "searxng",
                            "enabled": False,
                            "endpoint": "",
                            "allow_private_network": False,
                            "categories": "",
                            "language": "",
                            "time_range": "",
                        },
                        "x": {
                            "kind": "x_api",
                            "enabled": False,
                            "endpoint": "https://api.x.com",
                            "bearer_token": "",
                        },
                    }
                },
                "connectors": _default_connectors(),
                "mcp": {"servers": _default_mcp_servers()},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _mapping(value: object, path: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a mapping")
    return dict(value)


def _reject_unknown(raw: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{path} has unsupported fields: {', '.join(unknown)}")


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{path} must be a boolean")
    return value


def _search_provider_name(value: object) -> str:
    name = str(value).strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError(
            f"search provider name '{name}' must use lowercase letters, digits, and underscores"
        )
    return name


def _search_provider_config(
    raw: dict[str, Any],
    *,
    path: str,
) -> SearchProviderConfig:
    _reject_unknown(
        raw,
        {
            "kind",
            "enabled",
            "endpoint",
            "allow_private_network",
            "mcp_server",
            "bearer_token",
            "categories",
            "language",
            "time_range",
        },
        path,
    )
    kind = str(raw.get("kind") or "").strip().lower()
    if not kind:
        raise ValueError(f"{path}.kind is required")
    return SearchProviderConfig(
        kind=kind,
        enabled=_boolean(raw.get("enabled", True), f"{path}.enabled"),
        endpoint=str(raw.get("endpoint") or "").strip().rstrip("/"),
        allow_private_network=_boolean(
            raw.get("allow_private_network", False),
            f"{path}.allow_private_network",
        ),
        mcp_server=str(raw.get("mcp_server") or "").strip(),
        bearer_token=str(raw.get("bearer_token") or "").strip(),
        categories=str(raw.get("categories") or "").strip(),
        language=str(raw.get("language") or "").strip(),
        time_range=str(raw.get("time_range") or "").strip(),
    )


def _positive_float(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{path} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{path} must be a number") from None
    if parsed <= 0:
        raise ValueError(f"{path} must be greater than zero")
    return parsed


def _port(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{path} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{path} must be an integer") from None
    if parsed < 1 or parsed > 65535:
        raise ValueError(f"{path} must be between 1 and 65535")
    return parsed


def _provider_spec(provider: str, model_raw: dict) -> ProviderSpec:
    try:
        return get_provider_spec(provider)
    except ValueError:
        kind = str(model_raw.get("kind", "")).strip()
        if not kind:
            raise
        return ProviderSpec(
            name=provider,
            kind=kind,
            default_model=str(model_raw.get("model", "")),
            default_base_url=str(model_raw.get("api_base_url", "")).rstrip("/"),
        )


def _model_config(model_raw: dict, path: str = "model") -> ModelConfig:
    if "fallbacks" in model_raw:
        raise ValueError(
            f"{path}.fallbacks is unsupported; configure exactly one provider per model route"
        )
    _reject_unknown(model_raw, _MODEL_FIELDS, path)
    provider = str(model_raw.get("provider", DEFAULT_MODEL_PROVIDER)).strip()
    provider_spec = _provider_spec(provider, model_raw)
    raw_model = model_raw.get("model", provider_spec.default_model)
    model = str(raw_model).strip()
    api_base_url = str(model_raw.get("api_base_url", provider_spec.default_base_url)).rstrip("/")
    kind = str(model_raw.get("kind", provider_spec.kind)).strip()
    timeout_seconds = _positive_float(
        model_raw.get("timeout_seconds", DEFAULT_MODEL_TIMEOUT_SECONDS),
        f"{path}.timeout_seconds",
    )
    response_transport = str(model_raw.get("response_transport") or "json").strip()
    request_options = {
        str(key): value
        for key, value in _mapping(
            model_raw.get("request_options"), f"{path}.request_options"
        ).items()
    }
    routes_raw = _mapping(model_raw.get("routes"), f"{path}.routes")
    routes = {
        str(name): _model_config(
            _mapping(item, f"{path}.routes.{name}"),
            path=f"{path}.routes.{name}",
        )
        for name, item in routes_raw.items()
    }
    role_params_raw = _mapping(model_raw.get("role_params"), f"{path}.role_params")
    role_params = {
        str(role): _mapping(params, f"{path}.role_params.{role}")
        for role, params in role_params_raw.items()
    }
    return ModelConfig(
        provider=provider,
        model=model,
        api_base_url=api_base_url,
        api_key=str(model_raw.get("api_key") or "").strip(),
        kind=kind,
        timeout_seconds=timeout_seconds,
        response_transport=response_transport,
        request_options=request_options,
        routes=routes,
        role_params=role_params,
    )


def validate_config(config: NaviConfig, home: Path) -> list[str]:
    errors: list[str] = []

    def validate_model(m: ModelConfig, path: str):
        if not m.provider:
            errors.append(f"{path}.provider is empty")
            return

        try:
            spec = get_provider_spec(m.provider)
            kind = spec.kind
        except ValueError:
            kind = m.kind
            if not kind:
                errors.append(f"{path}.kind is required for custom provider '{m.provider}'")

        if kind and kind not in {"openai-compatible", "anthropic-compatible"}:
            errors.append(f"{path}.kind '{kind}' is unsupported")

        if m.response_transport not in {"json", "sse"}:
            errors.append(
                f"{path}.response_transport '{m.response_transport}' is unsupported; "
                "expected json or sse"
            )
        elif m.response_transport == "sse" and kind != "openai-compatible":
            errors.append(
                f"{path}.response_transport 'sse' requires kind 'openai-compatible'"
            )

        conflicts = sorted(RESERVED_REQUEST_OPTIONS.intersection(m.request_options))
        if conflicts:
            errors.append(
                f"{path}.request_options cannot override runtime fields: "
                + ", ".join(conflicts)
            )
        try:
            json.dumps(m.request_options)
        except (TypeError, ValueError) as exc:
            errors.append(f"{path}.request_options must be JSON-serializable: {exc}")

        for role, params in m.role_params.items():
            options = params.get("request_options")
            if options is None:
                continue
            if not isinstance(options, dict):
                errors.append(
                    f"{path}.role_params.{role}.request_options must be a mapping"
                )
                continue
            conflicts = sorted(RESERVED_REQUEST_OPTIONS.intersection(options))
            if conflicts:
                errors.append(
                    f"{path}.role_params.{role}.request_options cannot override "
                    "runtime fields: " + ", ".join(conflicts)
                )
            try:
                json.dumps(options)
            except (TypeError, ValueError) as exc:
                errors.append(
                    f"{path}.role_params.{role}.request_options must be "
                    f"JSON-serializable: {exc}"
                )

        if not m.api_key:
            errors.append(f"{path}.api_key is required")

    validate_model(config.model, "model")
    for role, route in config.model.routes.items():
        validate_model(route, f"model.routes.{role}")

    if not config.api.host:
        errors.append("api.host is required")
    if not config.api.api_key:
        errors.append("api.api_key is required")

    from .core_tools.web_search import validate_search_config

    errors.extend(validate_search_config(config, home))

    # Validate Connector Specs and configuration
    from .connector_registry import load_connector_adapters

    try:
        adapters = load_connector_adapters()
        for adapter in adapters:
            spec = adapter.spec
            if not spec.name:
                errors.append("connector spec has empty name")
            if not spec.surface:
                errors.append(f"connector spec '{spec.name}' has empty surface")
            if not spec.status_tool:
                errors.append(f"connector spec '{spec.name}' has empty status_tool")

            import importlib

            try:
                config_module = importlib.import_module(f"navi.{adapter.name}.config")
                load_cfg = getattr(config_module, f"load_{adapter.name}_config", None)
                if load_cfg:
                    cfg = load_cfg(home)
                    if getattr(cfg, "enabled", False):
                        dm_policy = getattr(cfg, "dm_policy", "open")
                        if dm_policy not in {"open", "disabled", "allowlist", "pairing"}:
                            errors.append(f"{adapter.name}.dm_policy '{dm_policy}' is invalid")
                        group_policy = getattr(cfg, "group_policy", None)
                        if group_policy is not None and group_policy not in {
                            "open",
                            "disabled",
                            "allowlist",
                            "pairing",
                        }:
                            errors.append(
                                f"{adapter.name}.group_policy '{group_policy}' is invalid"
                            )
            except (ModuleNotFoundError, AttributeError) as e:
                import logging

                logging.getLogger("navi.config").warning(
                    "Failed to validate connector %s: %s", adapter.name, e
                )
    except Exception as e:
        errors.append(f"connector load error: {e}")

    return errors
