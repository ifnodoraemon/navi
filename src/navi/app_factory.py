from __future__ import annotations

from pathlib import Path

from .config import load_config
from .paths import ensure_home
from .provider import build_provider
from .runtime import AgentRuntime


def build_runtime(home: Path | None = None) -> AgentRuntime:
    from .config import validate_config

    home = home or ensure_home()
    config = load_config(home)

    errors = validate_config(config, home)
    if errors:
        raise ValueError("invalid Navi configuration: " + "; ".join(errors))

    return AgentRuntime(home=home, provider=build_provider(config.model))
