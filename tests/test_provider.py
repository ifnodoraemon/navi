from __future__ import annotations

import httpx
import pytest

from navi.config import ModelConfig
from navi.provider import (
    ChatMessage,
    OpenAICompatibleProvider,
    build_provider,
    get_provider_spec,
    list_provider_specs,
    resolve_model_config,
)


def test_provider_registry_exposes_deepseek_defaults():
    spec = get_provider_spec("deepseek")

    assert spec.kind == "openai-compatible"
    assert spec.default_model == "deepseek-v4-pro"
    assert spec.default_base_url == "https://api.deepseek.com"
    assert "DEEPSEEK_API_KEY" in spec.api_key_env


def test_resolve_deepseek_config_normalizes_model_and_base_url():
    config = resolve_model_config(
        ModelConfig(
            provider="deepseek",
            model="mock",
            api_base_url="https://api.openai.com/v1",
            api_key="sk-test",
        )
    )

    assert config.provider == "deepseek"
    assert config.model == "deepseek-v4-pro"
    assert config.api_base_url == "https://api.deepseek.com"
    assert config.api_key == "sk-test"


def test_build_provider_accepts_openai_alias():
    provider = build_provider(
        ModelConfig(
            provider="openai",
            model="gpt-test",
            api_base_url="https://example.com/v1",
            api_key="sk-test",
        )
    )

    assert provider.__class__.__name__ == "OpenAICompatibleProvider"


def test_list_provider_specs_is_serializable():
    specs = list_provider_specs()

    assert {spec["name"] for spec in specs} >= {"mock", "openai-compatible", "deepseek"}


@pytest.mark.asyncio
async def test_openai_compatible_provider_posts_chat_completion():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "ok",
                            "reasoning_content": "internal reasoning should not be returned",
                        }
                    }
                ]
            },
        )

    provider = OpenAICompatibleProvider(
        ModelConfig(
            provider="deepseek",
            model="deepseek-v4-pro",
            api_base_url="https://api.deepseek.com",
            api_key="sk-test",
        ),
        get_provider_spec("deepseek"),
        transport=httpx.MockTransport(handler),
    )

    result = await provider.complete([ChatMessage("user", "hello")])

    assert result == "ok"
    assert str(requests[0].url) == "https://api.deepseek.com/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer sk-test"
    assert '"model":"deepseek-v4-pro"' in requests[0].content.decode()
