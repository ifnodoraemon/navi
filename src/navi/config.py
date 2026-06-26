from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .defaults import (
    DEFAULT_EXECUTION_PROVIDER,
    DEFAULT_EXECUTION_TIMEOUT_SECONDS,
    DEFAULT_LOCAL_SURFACE,
    DEFAULT_MODEL_TIMEOUT_SECONDS,
    DEFAULT_MODEL_MODEL,
    DEFAULT_MODEL_PROVIDER,
    DEFAULT_SERVICE_NAME,
)
from .paths import ensure_home
from .provider_specs import get_provider_spec
from .provider_specs import ProviderSpec


@dataclass
class ModelConfig:
    provider: str = DEFAULT_MODEL_PROVIDER
    model: str = DEFAULT_MODEL_MODEL
    api_base_url: str = ""
    api_key: str = ""
    kind: str = ""
    timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS
    fallbacks: list["ModelConfig"] = field(default_factory=list)
    routes: dict[str, "ModelConfig"] = field(default_factory=dict)
    role_params: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get_role_params(self, role: str) -> dict[str, Any]:
        defaults = {
            "planner": {"temperature": 0.0, "max_tokens": 4096},
            "responder": {"temperature": 0.6, "max_tokens": 16384},
            "consolidator": {"temperature": 0.1, "max_tokens": 2048},
        }
        base = defaults.get(role, {"temperature": 0.3, "max_tokens": 8192})
        base.update(self.role_params.get(role, {}))
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
class NaviConfig:
    model: ModelConfig = field(default_factory=ModelConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config root must be a mapping: {path}")
    return data


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _first_env(env: dict[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = env.get(name)
        if value:
            return value
    return ""


def load_config(home: Path | None = None) -> NaviConfig:
    home = home or ensure_home()
    raw = _read_yaml(home / "config.yaml")
    env_file = _load_env_file(home / "env")
    env = {**env_file, **os.environ}

    model_raw = raw.get("model") or {}
    runtime_raw = raw.get("runtime") or {}
    execution_raw = raw.get("execution") or {}
    model = _model_config(model_raw, env=env, allow_env_override=True)

    runtime = RuntimeConfig(
        service_name=str(
            env.get("NAVI_SERVICE_NAME", runtime_raw.get("service_name", DEFAULT_SERVICE_NAME))
        ),
        local_surface=str(
            env.get("NAVI_LOCAL_SURFACE", runtime_raw.get("local_surface", DEFAULT_LOCAL_SURFACE))
        ).strip(),
    )
    execution = ExecutionConfig(
        provider=str(
            env.get(
                "NAVI_EXECUTION_PROVIDER",
                execution_raw.get("provider", DEFAULT_EXECUTION_PROVIDER),
            )
        ).strip()
        or DEFAULT_EXECUTION_PROVIDER,
        timeout_seconds=_float_env(
            env.get(
                "NAVI_EXECUTION_TIMEOUT_SECONDS",
                execution_raw.get("timeout_seconds", DEFAULT_EXECUTION_TIMEOUT_SECONDS),
            ),
            default=DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        ),
    )
    return NaviConfig(model=model, runtime=runtime, execution=execution)


def write_default_config(home: Path | None = None) -> Path:
    home = home or ensure_home()
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
                },
                "runtime": {
                    "service_name": DEFAULT_SERVICE_NAME,
                    "local_surface": DEFAULT_LOCAL_SURFACE,
                },
                "execution": {
                    "provider": DEFAULT_EXECUTION_PROVIDER,
                    "timeout_seconds": DEFAULT_EXECUTION_TIMEOUT_SECONDS,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _float_env(value: object, *, default: float) -> float:
    try:
        return max(1.0, float(value))
    except (TypeError, ValueError):
        return default


def _provider_spec(provider: str, model_raw: dict, env: dict[str, str]) -> ProviderSpec:
    try:
        return get_provider_spec(provider)
    except ValueError:
        kind = str(env.get("NAVI_MODEL_KIND", model_raw.get("kind", ""))).strip()
        if not kind:
            raise
        api_key_env = tuple(model_raw.get("api_key_env") or ("NAVI_MODEL_API_KEY",))
        return ProviderSpec(
            name=provider,
            kind=kind,
            default_model=str(env.get("NAVI_MODEL", model_raw.get("model", ""))),
            default_base_url=str(
                env.get("NAVI_MODEL_API_BASE_URL", model_raw.get("api_base_url", ""))
            ).rstrip("/"),
            api_key_env=api_key_env,
        )


def _model_config(model_raw: dict, *, env: dict[str, str], allow_env_override: bool) -> ModelConfig:
    provider = str(
        env.get("NAVI_MODEL_PROVIDER", model_raw.get("provider", DEFAULT_MODEL_PROVIDER))
        if allow_env_override
        else model_raw.get("provider", DEFAULT_MODEL_PROVIDER)
    )
    provider_spec = _provider_spec(provider, model_raw, env if allow_env_override else {})
    raw_model = model_raw.get("model", provider_spec.default_model)
    if provider_spec.name != DEFAULT_MODEL_PROVIDER and raw_model == DEFAULT_MODEL_MODEL:
        raw_model = provider_spec.default_model
    model = str(env.get("NAVI_MODEL", raw_model) if allow_env_override else raw_model)
    api_base_url = str(
        env.get(
            "NAVI_MODEL_API_BASE_URL",
            model_raw.get("api_base_url", provider_spec.default_base_url),
        )
        if allow_env_override
        else model_raw.get("api_base_url", provider_spec.default_base_url)
    ).rstrip("/")
    kind = str(
        env.get("NAVI_MODEL_KIND", model_raw.get("kind", provider_spec.kind))
        if allow_env_override
        else model_raw.get("kind", provider_spec.kind)
    )
    timeout_seconds = _float_env(
        env.get(
            "NAVI_MODEL_TIMEOUT_SECONDS",
            model_raw.get("timeout_seconds", DEFAULT_MODEL_TIMEOUT_SECONDS),
        )
        if allow_env_override
        else model_raw.get("timeout_seconds", DEFAULT_MODEL_TIMEOUT_SECONDS),
        default=DEFAULT_MODEL_TIMEOUT_SECONDS,
    )
    fallbacks = [
        _model_config(item, env=env, allow_env_override=False)
        for item in model_raw.get("fallbacks") or []
        if isinstance(item, dict)
    ]
    routes = {
        str(name): _model_config(item, env=env, allow_env_override=False)
        for name, item in (model_raw.get("routes") or {}).items()
        if isinstance(item, dict)
    }
    role_params = model_raw.get("role_params") or {}
    return ModelConfig(
        provider=provider,
        model=model,
        api_base_url=api_base_url,
        api_key=str(model_raw.get("api_key") or _first_env(env, provider_spec.api_key_env)),
        kind=kind,
        timeout_seconds=timeout_seconds,
        fallbacks=fallbacks,
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
            api_key_env = spec.api_key_env
        except ValueError:
            kind = m.kind
            api_key_env = ("NAVI_MODEL_API_KEY",)
            if not kind:
                errors.append(f"{path}.kind is required for custom provider '{m.provider}'")

        if kind and kind not in {"openai-compatible", "anthropic-compatible"}:
            errors.append(f"{path}.kind '{kind}' is unsupported")

        if not m.api_key:
            env_hint = " or ".join(api_key_env)
            errors.append(
                f"{path}.api_key is empty and no environment override ({env_hint}) is set"
            )

    validate_model(config.model, "model")
    for idx, fb in enumerate(config.model.fallbacks):
        validate_model(fb, f"model.fallbacks[{idx}]")
    for role, route in config.model.routes.items():
        validate_model(route, f"model.routes.{role}")

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
