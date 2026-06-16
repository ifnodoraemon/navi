from __future__ import annotations

from .specs_data import DEFAULTS_SPEC

_DEFAULTS = DEFAULTS_SPEC

DEFAULT_SERVICE_NAME = str(_DEFAULTS["service_name"])
DEFAULT_EXECUTION_PROVIDER = str(_DEFAULTS["execution_provider"])
DEFAULT_EXECUTION_TIMEOUT_SECONDS = float(_DEFAULTS["execution_timeout_seconds"])
DEFAULT_EXECUTION_MOCK = bool(_DEFAULTS["execution_mock"])
DEFAULT_MODEL_PROVIDER = str(_DEFAULTS["model_provider"])
DEFAULT_MODEL_MODEL = str(_DEFAULTS["model_model"])
DEFAULT_MODEL_TIMEOUT_SECONDS = float(_DEFAULTS["model_timeout_seconds"])
DEFAULT_LOCAL_SURFACE = str(_DEFAULTS["local_surface"])
DEFAULT_AGENT_STEP_BUDGET = int(_DEFAULTS["agent_step_budget"])
DEFAULT_API_HOST = str(_DEFAULTS["api_host"])
DEFAULT_API_PORT = int(_DEFAULTS["api_port"])
