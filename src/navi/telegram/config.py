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
DEFAULT_TELEGRAM_ENABLED = bool(_DEFAULTS["enabled"])
DEFAULT_TELEGRAM_API_BASE_URL = str(_DEFAULTS["api_base_url"])
DEFAULT_TELEGRAM_DM_POLICY = str(_DEFAULTS["dm_policy"])


@dataclass
class TelegramConfig:
    enabled: bool = DEFAULT_TELEGRAM_ENABLED
    bot_token: str = ""
    api_base_url: str = DEFAULT_TELEGRAM_API_BASE_URL
    dm_policy: str = DEFAULT_TELEGRAM_DM_POLICY
    allowed_users: list[str] = field(default_factory=list)
    home_chat_id: str = ""


def load_telegram_config(home: Path) -> TelegramConfig:
    raw = _read_yaml(home / "config.yaml").get("telegram") or {}
    env_file = _load_env_file(home / "env")
    env = {**env_file, **os.environ}
    return TelegramConfig(
        enabled=str(env.get("NAVI_TELEGRAM_ENABLED", raw.get("enabled", DEFAULT_TELEGRAM_ENABLED))).lower()
        in {"1", "true", "yes", "on"},
        bot_token=str(env.get("TELEGRAM_BOT_TOKEN", raw.get("bot_token", ""))),
        api_base_url=str(env.get("TELEGRAM_API_BASE_URL", raw.get("api_base_url", DEFAULT_TELEGRAM_API_BASE_URL))).rstrip("/"),
        dm_policy=str(env.get("TELEGRAM_DM_POLICY", raw.get("dm_policy", DEFAULT_TELEGRAM_DM_POLICY))),
        allowed_users=_split_csv(env.get("TELEGRAM_ALLOWED_USERS")) or list(raw.get("allowed_users", []) or []),
        home_chat_id=str(env.get("TELEGRAM_HOME_CHAT_ID", raw.get("home_chat_id", ""))),
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
