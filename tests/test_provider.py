from __future__ import annotations

import json

import httpx
import pytest

from navi.config import ModelConfig
from navi.provider import (
    AnthropicCompatibleProvider,
    ChatMessage,
    OpenAICompatibleProvider,
    build_provider,
    get_provider_spec,
    list_provider_specs,
    resolve_model_config,
)
from navi.runtime import AgentRuntime
from navi.engine import HernessEngine


def test_provider_registry_exposes_deepseek_defaults():
    spec = get_provider_spec("deepseek")

    assert spec.kind == "openai-compatible"
    assert spec.default_model == "deepseek-v4-pro"
    assert spec.default_base_url == "https://api.deepseek.com"
    assert spec.structured_output == "json_object"
    assert "DEEPSEEK_API_KEY" in spec.api_key_env


def test_provider_registry_exposes_anthropic_tool_schema_output():
    spec = get_provider_spec("anthropic")

    assert spec.kind == "anthropic-compatible"
    assert spec.structured_output == "tool_schema"


def test_resolve_deepseek_config_uses_provider_defaults_when_empty():
    config = resolve_model_config(
        ModelConfig(
            provider="deepseek",
            model="",
            api_base_url="",
            api_key="sk-test",
        )
    )

    assert config.provider == "deepseek"
    assert config.model == "deepseek-v4-pro"
    assert config.api_base_url == "https://api.deepseek.com"
    assert config.api_key == "sk-test"
    assert config.timeout_seconds == 60


def test_build_provider_accepts_openai_compatible_provider():
    provider = build_provider(
        ModelConfig(
            provider="openai-compatible",
            model="gpt-test",
            api_base_url="https://example.com/v1",
            api_key="sk-test",
        )
    )

    assert provider.__class__.__name__ == "ModelPool"
    assert provider.default.__class__.__name__ == "OpenAICompatibleProvider"


def test_list_provider_specs_is_serializable():
    specs = list_provider_specs()

    assert {spec["name"] for spec in specs} >= {"mock", "openai-compatible", "deepseek", "anthropic"}


def test_build_provider_accepts_anthropic_provider():
    provider = build_provider(
        ModelConfig(
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            api_base_url="https://api.anthropic.com/v1",
            api_key="sk-test",
        )
    )

    assert provider.default.__class__.__name__ == "AnthropicCompatibleProvider"


def test_build_provider_accepts_custom_openai_compatible_provider():
    provider = build_provider(
        ModelConfig(
            provider="private-gateway",
            kind="openai-compatible",
            model="local-agent-model",
            api_base_url="http://localhost:11434/v1",
            api_key="sk-local",
        )
    )

    assert provider.default.__class__.__name__ == "OpenAICompatibleProvider"


@pytest.mark.asyncio
async def test_model_pool_routes_by_role_with_fallback():
    provider = build_provider(
        ModelConfig(
            provider="mock",
            model="mock",
            routes={
                "planner": ModelConfig(
                    provider="private-planner",
                    kind="openai-compatible",
                    model="planner-model",
                    api_base_url="http://planner.local/v1",
                    fallbacks=[ModelConfig(provider="mock", model="mock")],
                )
            },
        )
    )

    result = await provider.complete_for("planner", [ChatMessage("user", "route me")])

    assert provider.__class__.__name__ == "ModelPool"
    assert result == "route me"


@pytest.mark.asyncio
async def test_herness_engine_uses_planner_route(tmp_path):
    provider = build_provider(
        ModelConfig(
            provider="mock",
            model="mock",
            routes={
                "planner": ModelConfig(
                    provider="private-planner",
                    kind="openai-compatible",
                    model="planner-model",
                    api_base_url="http://planner.local/v1",
                    fallbacks=[ModelConfig(provider="mock", model="mock")],
                )
            },
        )
    )
    runtime = AgentRuntime(home=tmp_path, provider=provider)
    agent = HernessEngine(home=tmp_path, runtime=runtime, project_dir=tmp_path)

    result = await agent.handle("hello", peer_id="peer", sender_id="sender", source="test")

    assert result.text == "hello"
    assert provider.__class__.__name__ == "ModelPool"


@pytest.mark.asyncio
async def test_fallback_provider_tries_next_model_when_primary_fails():
    provider = build_provider(
        ModelConfig(
            provider="private-primary",
            kind="openai-compatible",
            model="primary-model",
            api_base_url="http://primary.local/v1",
            fallbacks=[ModelConfig(provider="mock", model="mock")],
        )
    )

    result = await provider.complete_for("default", [ChatMessage("user", "hello")])

    assert provider.default.__class__.__name__ == "FallbackProvider"
    assert result == "hello"


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
            timeout_seconds=13,
        ),
        get_provider_spec("deepseek"),
        transport=httpx.MockTransport(handler),
    )

    result = await provider.complete([ChatMessage("user", "hello")])

    assert result == "ok"
    assert str(requests[0].url) == "https://api.deepseek.com/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer sk-test"
    assert '"model":"deepseek-v4-pro"' in requests[0].content.decode()
    assert requests[0].extensions["timeout"]["connect"] == 13
    assert provider.config.timeout_seconds == 13


@pytest.mark.asyncio
async def test_openai_provider_sends_json_schema_response_format():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    provider = OpenAICompatibleProvider(
        ModelConfig(
            provider="openai-compatible",
            model="gpt-test",
            api_base_url="https://api.openai.com/v1",
            api_key="sk-test",
        ),
        get_provider_spec("openai-compatible"),
        transport=httpx.MockTransport(handler),
    )

    await provider.complete(
        [ChatMessage("user", "return json")],
        output_schema={
            "name": "navi_test",
            "strict": False,
            "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
        },
    )

    body = json.loads(requests[0].content)
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["name"] == "navi_test"
    assert body["response_format"]["json_schema"]["schema"]["properties"]["ok"]["type"] == "boolean"


@pytest.mark.asyncio
async def test_deepseek_provider_sends_json_object_response_format():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

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

    await provider.complete(
        [ChatMessage("user", "hello")],
        output_schema={"name": "navi_test", "schema": {"type": "object"}},
    )

    body = json.loads(requests[0].content)
    assert body["response_format"] == {"type": "json_object"}
    assert "JSON mode" in body["messages"][0]["content"]


@pytest.mark.asyncio
async def test_anthropic_provider_posts_messages_request():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "anthropic ok"}]},
        )

    provider = AnthropicCompatibleProvider(
        ModelConfig(
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            api_base_url="https://api.anthropic.com/v1",
            api_key="sk-test",
        ),
        get_provider_spec("anthropic"),
        transport=httpx.MockTransport(handler),
    )

    result = await provider.complete([ChatMessage("system", "sys"), ChatMessage("user", "hello")])

    assert result == "anthropic ok"
    assert str(requests[0].url) == "https://api.anthropic.com/v1/messages"
    assert requests[0].headers["x-api-key"] == "sk-test"
    body = requests[0].content.decode()
    assert '"system":"sys"' in body
    assert '"messages":[{"role":"user","content":"hello"}]' in body


@pytest.mark.asyncio
async def test_anthropic_provider_sends_tool_schema_and_extracts_tool_input():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_test",
                        "name": "navi_test",
                        "input": {"ok": True, "reason": "schema matched"},
                    }
                ]
            },
        )

    provider = AnthropicCompatibleProvider(
        ModelConfig(
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            api_base_url="https://api.anthropic.com/v1",
            api_key="sk-test",
        ),
        get_provider_spec("anthropic"),
        transport=httpx.MockTransport(handler),
    )

    result = await provider.complete(
        [ChatMessage("user", "hello")],
        output_schema={
            "name": "navi_test",
            "schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["ok", "reason"],
                "additionalProperties": False,
            },
        },
    )

    body = json.loads(requests[0].content)
    assert body["tools"][0]["name"] == "navi_test"
    assert body["tools"][0]["input_schema"]["properties"]["ok"]["type"] == "boolean"
    assert body["tool_choice"] == {"type": "tool", "name": "navi_test"}
    assert json.loads(result) == {"ok": True, "reason": "schema matched"}
