from __future__ import annotations

from .spec_loader import load_spec

_DEFAULTS = load_spec("defaults.yaml")

DEFAULT_SERVICE_NAME = str(_DEFAULTS["service_name"])
DEFAULT_EXECUTION_PROVIDER = str(_DEFAULTS["execution_provider"])
DEFAULT_EXECUTION_TIMEOUT_SECONDS = float(_DEFAULTS["execution_timeout_seconds"])
DEFAULT_EXECUTION_MOCK = bool(_DEFAULTS["execution_mock"])
DEFAULT_MODEL_PROVIDER = str(_DEFAULTS["model_provider"])
DEFAULT_MODEL_MODEL = str(_DEFAULTS["model_model"])
DEFAULT_RUNTIME_WEB_URL = str(_DEFAULTS["runtime_web_url"])
DEFAULT_LOCAL_SURFACE = str(_DEFAULTS["local_surface"])
DEFAULT_WEB_HOST = str(_DEFAULTS["web_host"])
DEFAULT_WEB_PORT = int(_DEFAULTS["web_port"])
