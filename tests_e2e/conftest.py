from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from navi.api import create_app
from navi.config import write_default_config


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
