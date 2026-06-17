from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from navi.api import create_app
from navi.config import write_default_config


# Keys that indicate a real model provider is configured for live e2e runs.
_LIVE_LLM_KEYS = ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "NAVI_MODEL_API_KEY")


def _has_live_llm_credentials() -> bool:
    return any(os.environ.get(key) for key in _LIVE_LLM_KEYS)


def pytest_collection_modifyitems(config, items):
    """Skip live_llm e2e tests only when no real provider credentials are set.

    e2e tests exercise the real model path by default; they are skipped (not
    faked) when credentials are absent so CI without a key stays green without
    introducing a runtime simulation mode.
    """
    if _has_live_llm_credentials():
        return
    skip = pytest.mark.skip(reason="no real LLM credentials set (live_llm e2e)")
    for item in items:
        if "live_llm" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def navi_home(tmp_path, monkeypatch):
    home_dir = tmp_path / ".navi"
    home_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("NAVI_HOME", str(home_dir))
    write_default_config(home_dir)
    api_key_path = home_dir / "api_key"
    api_key_path.write_text("test_key", encoding="utf-8")
    return home_dir


@pytest.fixture
def api_client(navi_home, monkeypatch):
    api_key_path = navi_home / "api_key"
    api_key = api_key_path.read_text(encoding="utf-8").strip()
    monkeypatch.setenv("NAVI_API_KEY", api_key)
    
    app = create_app(navi_home)
    client = TestClient(app, headers={"X-API-Key": api_key})
    return client
