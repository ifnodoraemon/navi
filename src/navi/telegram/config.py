from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from navi.config import load_config
from navi.defaults import (
    DEFAULT_TELEGRAM_API_BASE_URL,
    DEFAULT_TELEGRAM_DM_POLICY,
    DEFAULT_TELEGRAM_ENABLED,
)

_FIELDS = {
    "enabled",
    "bot_token",
    "api_base_url",
    "dm_policy",
    "allowed_users",
    "home_chat_id",
}


@dataclass
class TelegramConfig:
    enabled: bool = DEFAULT_TELEGRAM_ENABLED
    bot_token: str = ""
    api_base_url: str = DEFAULT_TELEGRAM_API_BASE_URL
    dm_policy: str = DEFAULT_TELEGRAM_DM_POLICY
    allowed_users: list[str] = field(default_factory=list)
    home_chat_id: str = ""


def load_telegram_config(home: Path) -> TelegramConfig:
    raw = load_config(home).connectors["telegram"]
    unknown = sorted(set(raw) - _FIELDS)
    if unknown:
        raise ValueError(f"connectors.telegram has unsupported fields: {', '.join(unknown)}")
    return TelegramConfig(
        enabled=_bool(raw.get("enabled"), "connectors.telegram.enabled"),
        bot_token=str(raw.get("bot_token") or ""),
        api_base_url=str(raw.get("api_base_url") or "").rstrip("/"),
        dm_policy=str(raw.get("dm_policy") or ""),
        allowed_users=_string_list(raw.get("allowed_users"), "connectors.telegram.allowed_users"),
        home_chat_id=str(raw.get("home_chat_id") or ""),
    )


def _bool(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{path} must be a boolean")
    return value


def _string_list(value: object, path: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{path} must be a list of strings")
    return list(value)
