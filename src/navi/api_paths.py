from __future__ import annotations

from .specs_data import API_PATHS_SPEC

API_PATHS: dict[str, str] = {str(key): str(value) for key, value in API_PATHS_SPEC.items()}


def api_path(name: str) -> str:
    return API_PATHS[name]
