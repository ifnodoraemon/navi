from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from navi.api import create_app
from navi.config import load_config, validate_config, write_default_config


def _live_llm_enabled() -> bool:
    return os.environ.get("NAVI_LIVE_LLM_TESTS") == "1"


def pytest_collection_modifyitems(config, items):
    """Skip live_llm e2e tests only when no real provider credentials are set.

    e2e tests exercise the real model path by default; they are skipped (not
    faked) when credentials are absent so CI without a key stays green without
    introducing a runtime simulation mode.
    """
    if _live_llm_enabled():
        return
    skip = pytest.mark.skip(reason="set NAVI_LIVE_LLM_TESTS=1 to run live_llm e2e")
    for item in items:
        if "live_llm" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def navi_home(tmp_path, monkeypatch, request):
    if "live_llm" in request.node.keywords and _live_llm_enabled():
        home_dir = Path(os.environ.get("NAVI_HOME", ".navi")).expanduser().resolve()
        config = load_config(home_dir)
        errors = validate_config(config, home_dir)
        if errors:
            pytest.fail("invalid live Navi configuration: " + "; ".join(errors))
        return home_dir

    home_dir = tmp_path / ".navi"
    home_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("NAVI_HOME", str(home_dir))
    path = write_default_config(home_dir)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw.setdefault("model", {})["api_key"] = "test-model-key"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    path.chmod(0o600)
    return home_dir


@pytest.fixture
def api_client(navi_home):
    api_key = load_config(navi_home).api.api_key
    app = create_app(navi_home)
    with TestClient(app, headers={"X-API-Key": api_key}) as client:
        yield client
