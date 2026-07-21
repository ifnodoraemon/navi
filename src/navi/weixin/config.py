from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from navi.config import load_config
from navi.defaults import (
    DEFAULT_WEIXIN_BASE_URL,
    DEFAULT_WEIXIN_CDN_BASE_URL,
    DEFAULT_WEIXIN_DM_POLICY,
    DEFAULT_WEIXIN_ENABLED,
    DEFAULT_WEIXIN_GROUP_POLICY,
)

_FIELDS = {
    "enabled",
    "account_id",
    "token",
    "base_url",
    "cdn_base_url",
    "dm_policy",
    "allowed_users",
    "group_policy",
    "group_allowed_users",
    "home_channel",
}


@dataclass
class WeixinConfig:
    enabled: bool = DEFAULT_WEIXIN_ENABLED
    account_id: str = ""
    token: str = ""
    base_url: str = DEFAULT_WEIXIN_BASE_URL
    cdn_base_url: str = DEFAULT_WEIXIN_CDN_BASE_URL
    dm_policy: str = DEFAULT_WEIXIN_DM_POLICY
    allowed_users: list[str] = field(default_factory=list)
    group_policy: str = DEFAULT_WEIXIN_GROUP_POLICY
    group_allowed_users: list[str] = field(default_factory=list)
    home_channel: str = ""


def load_weixin_config(home: Path) -> WeixinConfig:
    raw = load_config(home).connectors["weixin"]
    unknown = sorted(set(raw) - _FIELDS)
    if unknown:
        raise ValueError(f"connectors.weixin has unsupported fields: {', '.join(unknown)}")
    return WeixinConfig(
        enabled=_bool(raw.get("enabled"), "connectors.weixin.enabled"),
        account_id=str(raw.get("account_id") or ""),
        token=str(raw.get("token") or ""),
        base_url=str(raw.get("base_url") or "").rstrip("/"),
        cdn_base_url=str(raw.get("cdn_base_url") or "").rstrip("/"),
        dm_policy=str(raw.get("dm_policy") or ""),
        allowed_users=_string_list(raw.get("allowed_users"), "connectors.weixin.allowed_users"),
        group_policy=str(raw.get("group_policy") or ""),
        group_allowed_users=_string_list(
            raw.get("group_allowed_users"),
            "connectors.weixin.group_allowed_users",
        ),
        home_channel=str(raw.get("home_channel") or ""),
    )


def _bool(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{path} must be a boolean")
    return value


def _string_list(value: object, path: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{path} must be a list of strings")
    return list(value)
