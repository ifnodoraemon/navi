from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .paths import ensure_home
from .provider_specs import get_provider_spec


@dataclass
class ModelConfig:
    provider: str = "mock"
    model: str = "mock"
    api_base_url: str = "https://api.openai.com/v1"
    api_key: str = ""


@dataclass
class WeixinConfig:
    enabled: bool = False
    account_id: str = ""
    token: str = ""
    base_url: str = "https://ilinkai.weixin.qq.com"
    dm_policy: str = "open"
    allowed_users: list[str] = field(default_factory=list)
    group_policy: str = "disabled"
    group_allowed_users: list[str] = field(default_factory=list)
    home_channel: str = ""


@dataclass
class RuntimeConfig:
    service_name: str = "navi.service"
    web_url: str = ""


@dataclass
class NaviConfig:
    model: ModelConfig = field(default_factory=ModelConfig)
    weixin: WeixinConfig = field(default_factory=WeixinConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


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
    weixin_raw = raw.get("weixin") or {}
    runtime_raw = raw.get("runtime") or {}
    provider = str(env.get("NAVI_MODEL_PROVIDER", model_raw.get("provider", "mock")))
    provider_spec = get_provider_spec(provider)
    raw_model = model_raw.get("model", provider_spec.default_model)
    if provider_spec.name != "mock" and raw_model == "mock":
        raw_model = provider_spec.default_model

    model = ModelConfig(
        provider=provider,
        model=str(env.get("NAVI_MODEL", raw_model)),
        api_base_url=str(
            env.get(
                "NAVI_MODEL_API_BASE_URL",
                model_raw.get("api_base_url", provider_spec.default_base_url),
            )
        ).rstrip("/"),
        api_key=str(model_raw.get("api_key") or _first_env(env, provider_spec.api_key_env)),
    )

    weixin = WeixinConfig(
        enabled=str(env.get("NAVI_WEIXIN_ENABLED", weixin_raw.get("enabled", False))).lower()
        in {"1", "true", "yes", "on"},
        account_id=str(env.get("WEIXIN_ACCOUNT_ID", weixin_raw.get("account_id", ""))),
        token=str(env.get("WEIXIN_TOKEN", weixin_raw.get("token", ""))),
        base_url=str(env.get("WEIXIN_BASE_URL", weixin_raw.get("base_url", "https://ilinkai.weixin.qq.com"))).rstrip("/"),
        dm_policy=str(env.get("WEIXIN_DM_POLICY", weixin_raw.get("dm_policy", "open"))),
        allowed_users=_split_csv(env.get("WEIXIN_ALLOWED_USERS"))
        or list(weixin_raw.get("allowed_users", []) or []),
        group_policy=str(env.get("WEIXIN_GROUP_POLICY", weixin_raw.get("group_policy", "disabled"))),
        group_allowed_users=_split_csv(env.get("WEIXIN_GROUP_ALLOWED_USERS"))
        or list(weixin_raw.get("group_allowed_users", []) or []),
        home_channel=str(env.get("WEIXIN_HOME_CHANNEL", weixin_raw.get("home_channel", ""))),
    )
    runtime = RuntimeConfig(
        service_name=str(env.get("NAVI_SERVICE_NAME", runtime_raw.get("service_name", "navi.service"))),
        web_url=str(env.get("NAVI_WEB_URL", runtime_raw.get("web_url", ""))).strip(),
    )
    return NaviConfig(model=model, weixin=weixin, runtime=runtime)


def write_default_config(home: Path | None = None) -> Path:
    home = home or ensure_home()
    path = home / "config.yaml"
    if path.exists():
        return path
    path.write_text(
        yaml.safe_dump(
            {
                "model": {"provider": "mock", "model": "mock"},
                "weixin": {
                    "enabled": False,
                    "base_url": "https://ilinkai.weixin.qq.com",
                    "dm_policy": "open",
                    "group_policy": "disabled",
                },
                "runtime": {
                    "service_name": "navi.service",
                    "web_url": "",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path
