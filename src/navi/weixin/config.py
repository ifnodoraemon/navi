from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config root must be a mapping: {path}")
    return data


_DEFAULTS = _read_yaml(Path(__file__).with_name("specs") / "defaults.yaml")
DEFAULT_WEIXIN_BASE_URL = str(_DEFAULTS["base_url"])
DEFAULT_WEIXIN_ENABLED = bool(_DEFAULTS["enabled"])
DEFAULT_WEIXIN_DM_POLICY = str(_DEFAULTS["dm_policy"])
DEFAULT_WEIXIN_GROUP_POLICY = str(_DEFAULTS["group_policy"])


@dataclass
class WeixinConfig:
    enabled: bool = DEFAULT_WEIXIN_ENABLED
    account_id: str = ""
    token: str = ""
    base_url: str = DEFAULT_WEIXIN_BASE_URL
    dm_policy: str = DEFAULT_WEIXIN_DM_POLICY
    allowed_users: list[str] = field(default_factory=list)
    group_policy: str = DEFAULT_WEIXIN_GROUP_POLICY
    group_allowed_users: list[str] = field(default_factory=list)
    home_channel: str = ""


def load_weixin_config(home: Path) -> WeixinConfig:
    raw = _read_yaml(home / "config.yaml").get("weixin") or {}
    env_file = _load_env_file(home / "env")
    env = {**env_file, **os.environ}
    return WeixinConfig(
        enabled=str(env.get("NAVI_WEIXIN_ENABLED", raw.get("enabled", DEFAULT_WEIXIN_ENABLED))).lower()
        in {"1", "true", "yes", "on"},
        account_id=str(env.get("WEIXIN_ACCOUNT_ID", raw.get("account_id", ""))),
        token=str(env.get("WEIXIN_TOKEN", raw.get("token", ""))),
        base_url=str(env.get("WEIXIN_BASE_URL", raw.get("base_url", DEFAULT_WEIXIN_BASE_URL))).rstrip("/"),
        dm_policy=str(env.get("WEIXIN_DM_POLICY", raw.get("dm_policy", DEFAULT_WEIXIN_DM_POLICY))),
        allowed_users=_split_csv(env.get("WEIXIN_ALLOWED_USERS")) or list(raw.get("allowed_users", []) or []),
        group_policy=str(env.get("WEIXIN_GROUP_POLICY", raw.get("group_policy", DEFAULT_WEIXIN_GROUP_POLICY))),
        group_allowed_users=_split_csv(env.get("WEIXIN_GROUP_ALLOWED_USERS"))
        or list(raw.get("group_allowed_users", []) or []),
        home_channel=str(env.get("WEIXIN_HOME_CHANNEL", raw.get("home_channel", ""))),
    )


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


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
