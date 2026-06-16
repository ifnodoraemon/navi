from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from navi.api import create_app
from navi.config import write_default_config
from navi.provider import ChatMessage, MockProvider, ModelPool
from navi.runtime import AgentRuntime


class ScriptedProvider(MockProvider):
    def __init__(self, responses: str | list[str]):
        if isinstance(responses, list):
            self.responses = list(responses)
        else:
            self.responses = [responses]
        self.messages: list[ChatMessage] = []
        self.all_messages: list[list[ChatMessage]] = []

    async def complete(
        self, messages: list[ChatMessage], *, output_schema: dict[str, Any] | None = None
    ) -> str:
        self.messages = messages
        self.all_messages.append(messages)
        if not self.responses:
            return '{"tool":"final.answer","permission":"read","args":{"message":"dummy timeout"},"confidence":0.95,"reason":"out of responses"}'
        response = self.responses.pop(0)
        if isinstance(response, str) and "TASK_ID" in response:
            for message in reversed(messages):
                match = re.search(r'"run_id":\s*"([^"]+)"', message.content)
                if match:
                    response = response.replace("TASK_ID", match.group(1))
                    break
        return response


@pytest.fixture(name="ScriptedProvider")
def _scripted_provider():
    return ScriptedProvider


@pytest.fixture
def mock_home(tmp_path, monkeypatch):
    home_dir = tmp_path / ".navi"
    home_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("NAVI_HOME", str(home_dir))
    write_default_config(home_dir)
    api_key_path = home_dir / "api_key"
    api_key_path.write_text("test_key", encoding="utf-8")
    return home_dir


@pytest.fixture
def runtime_builder(mock_home, monkeypatch):
    import navi.api as api_module

    def _build(provider: Any) -> AgentRuntime:
        if isinstance(provider, ModelPool):
            pool = provider
        elif isinstance(provider, (list, str)):
            pool = ModelPool(default=ScriptedProvider(provider))
        else:
            pool = ModelPool(default=provider)
        
        runtime = AgentRuntime(home=mock_home, provider=pool)
        monkeypatch.setattr(
            api_module,
            "build_runtime",
            lambda home=None: runtime,
        )
        return runtime
    return _build


@pytest.fixture
def api_client(mock_home, monkeypatch):
    api_key_path = mock_home / "api_key"
    api_key = api_key_path.read_text(encoding="utf-8").strip()
    monkeypatch.setenv("NAVI_API_KEY", api_key)
    
    app = create_app(mock_home)
    client = TestClient(app, headers={"X-API-Key": api_key})
    return client
