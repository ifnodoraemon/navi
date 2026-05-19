from __future__ import annotations

from .spec_loader import load_spec

API_PATHS: dict[str, str] = {str(key): str(value) for key, value in load_spec("api.yaml").items()}


def api_path(name: str) -> str:
    return API_PATHS[name]
