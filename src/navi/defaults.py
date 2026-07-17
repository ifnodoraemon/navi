from __future__ import annotations

from typing import Any

from .specs_data import DEFAULTS_SPEC

_DEFAULTS = DEFAULTS_SPEC

DEFAULT_SERVICE_NAME = str(_DEFAULTS["service_name"])
DEFAULT_EXECUTION_PROVIDER = str(_DEFAULTS["execution_provider"])
DEFAULT_EXECUTION_TIMEOUT_SECONDS = float(_DEFAULTS["execution_timeout_seconds"])
DEFAULT_MODEL_PROVIDER = str(_DEFAULTS["model_provider"])
DEFAULT_MODEL_MODEL = str(_DEFAULTS["model_model"])
DEFAULT_MODEL_TIMEOUT_SECONDS = float(_DEFAULTS["model_timeout_seconds"])
DEFAULT_MODEL_ROLE_PARAMS: dict[str, dict[str, Any]] = {
    str(role): dict(params)
    for role, params in dict(_DEFAULTS["model_role_params"]).items()
    if isinstance(params, dict)
}
DEFAULT_LOCAL_SURFACE = str(_DEFAULTS["local_surface"])
DEFAULT_API_HOST = str(_DEFAULTS["api_host"])
DEFAULT_API_PORT = int(_DEFAULTS["api_port"])
